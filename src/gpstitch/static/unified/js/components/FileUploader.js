/**
 * FileUploader - Handles video and GPS file uploads
 * Two separate fields:
 * - Video field: MP4/MOV files
 * - GPS field: GPX/FIT files
 *
 * Modes determined by what's loaded:
 * - Video only = GoPro with embedded GPS
 * - GPS only = overlay-only mode
 * - Video + GPS = merge mode
 */

class FileUploader {
    constructor(container, fileInput, state) {
        this.container = container;
        this.fileInput = fileInput;
        this.state = state;

        this.isUploading = false;
        this.localMode = false;
        this.browseAvailable = false;

        this._init();
    }

    async _init() {
        // Check if local mode is enabled
        try {
            const response = await fetch('/api/config');
            if (response.ok) {
                const config = await response.json();
                this.localMode = config.local_mode;
            }
        } catch (e) {
            console.warn('Could not fetch config, using default mode');
        }

        // A native file picker only exists on some platforms; hide the button
        // rather than offering one that always errors.
        if (this.localMode) {
            try {
                const response = await fetch('/api/browse/available');
                if (response.ok) {
                    this.browseAvailable = (await response.json()).available;
                }
            } catch (e) {
                console.warn('Could not check file browser availability');
            }
        }

        this._render();
        this._attachEventListeners();
    }

    _render() {
        this.container.innerHTML = `
            <!-- Video Field -->
            <div class="file-field" id="video-field">
                <div class="file-field-header">
                    <span class="file-field-label">Video</span>
                    <span class="file-field-hint">MP4 (optional)</span>
                </div>
                ${this.localMode ? `
                    <div class="file-field-input-row">
                        <input type="text" id="video-path-input" class="file-path-input" placeholder="/path/to/video.mp4">
                        ${this.browseAvailable ? `
                            <button id="video-browse-btn" class="btn btn-sm btn-secondary file-browse-btn" title="Browse for a video">Browse…</button>
                        ` : ''}
                        <button id="video-load-btn" class="btn btn-sm btn-primary">Load</button>
                    </div>
                ` : `
                    <div id="video-drop-zone" class="file-drop-zone">
                        <span class="drop-zone-text">Drop MP4/MOV or click</span>
                    </div>
                    <input type="file" id="video-file-input" accept=".mp4,.mov" class="visually-hidden">
                `}
                <div id="video-file-info" class="file-info" style="display: none;">
                    <span class="file-info-name"></span>
                    <button class="file-clear-btn" data-field="video" title="Remove">&times;</button>
                </div>
            </div>

            <!-- GPS Field -->
            <div class="file-field" id="gps-field">
                <div class="file-field-header">
                    <span class="file-field-label">GPS Data</span>
                    <span class="file-field-hint">GPX/FIT/SRT (optional)</span>
                </div>
                ${this.localMode ? `
                    <div class="file-field-input-row">
                        <input type="text" id="gps-path-input" class="file-path-input" placeholder="/path/to/track.gpx or .srt">
                        ${this.browseAvailable ? `
                            <button id="gps-browse-btn" class="btn btn-sm btn-secondary file-browse-btn" title="Browse for GPS data">Browse…</button>
                        ` : ''}
                        <button id="gps-load-btn" class="btn btn-sm btn-primary">Load</button>
                    </div>
                ` : `
                    <div id="gps-drop-zone" class="file-drop-zone">
                        <span class="drop-zone-text">Drop GPX/FIT/SRT or click</span>
                    </div>
                    <input type="file" id="gps-file-input" accept=".gpx,.fit,.srt" class="visually-hidden">
                `}
                <div id="gps-file-info" class="file-info" style="display: none;">
                    <span class="file-info-name"></span>
                    <button class="file-clear-btn" data-field="gps" title="Remove">&times;</button>
                </div>
            </div>

            <!-- Mode indicator -->
            <div id="mode-indicator" class="mode-indicator" style="display: none;"></div>
        `;

        // Cache DOM references
        this.videoField = document.getElementById('video-field');
        this.gpsField = document.getElementById('gps-field');
        this.videoFileInfo = document.getElementById('video-file-info');
        this.gpsFileInfo = document.getElementById('gps-file-info');
        this.modeIndicator = document.getElementById('mode-indicator');

        if (this.localMode) {
            this.videoPathInput = document.getElementById('video-path-input');
            this.gpsPathInput = document.getElementById('gps-path-input');
        } else {
            this.videoDropZone = document.getElementById('video-drop-zone');
            this.gpsDropZone = document.getElementById('gps-drop-zone');
            this.videoFileInput = document.getElementById('video-file-input');
            this.gpsFileInput = document.getElementById('gps-file-input');
        }
    }

    _attachEventListeners() {
        if (this.localMode) {
            // Local mode: path inputs
            document.getElementById('video-load-btn')?.addEventListener('click', () => this._loadLocalFile('video'));
            document.getElementById('gps-load-btn')?.addEventListener('click', () => this._loadLocalFile('gps'));

            document.getElementById('video-browse-btn')?.addEventListener('click', () => this._browseFor('video'));
            document.getElementById('gps-browse-btn')?.addEventListener('click', () => this._browseFor('gps'));

            this.videoPathInput?.addEventListener('keypress', (e) => {
                if (e.key === 'Enter') this._loadLocalFile('video');
            });
            this.gpsPathInput?.addEventListener('keypress', (e) => {
                if (e.key === 'Enter') this._loadLocalFile('gps');
            });

            // Auto-clean quotes on paste
            [this.videoPathInput, this.gpsPathInput].forEach(input => {
                input?.addEventListener('paste', () => {
                    setTimeout(() => {
                        input.value = this._cleanPath(input.value);
                    }, 0);
                });
            });
        } else {
            // Upload mode: drop zones
            this._setupDropZone(this.videoDropZone, this.videoFileInput, 'video');
            this._setupDropZone(this.gpsDropZone, this.gpsFileInput, 'gps');
        }

        // Clear buttons. Removing a file talks to the server, so the button
        // shows it is working rather than looking like the click was missed.
        document.querySelectorAll('.file-clear-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                const field = e.target.dataset.field;
                window.Busy.run(btn, () => this._clearFile(field));
            });
        });

        // State events
        this.state.on('session:changed', () => this._updateUI());
        this.state.on('session:cleared', () => this._updateUI());
        this.state.on('files:changed', () => this._updateUI());
    }

    _setupDropZone(dropZone, fileInput, type) {
        if (!dropZone || !fileInput) return;

        dropZone.addEventListener('click', () => fileInput.click());

        dropZone.addEventListener('dragover', (e) => {
            e.preventDefault();
            dropZone.classList.add('dragover');
        });

        dropZone.addEventListener('dragleave', (e) => {
            e.preventDefault();
            dropZone.classList.remove('dragover');
        });

        dropZone.addEventListener('drop', (e) => {
            e.preventDefault();
            dropZone.classList.remove('dragover');
            if (e.dataTransfer.files.length > 0) {
                this._uploadFile(e.dataTransfer.files[0], type);
            }
        });

        fileInput.addEventListener('change', (e) => {
            if (e.target.files.length > 0) {
                this._uploadFile(e.target.files[0], type);
            }
            e.target.value = '';
        });
    }

    _cleanPath(path) {
        if (!path) return '';
        path = path.trim();
        if ((path.startsWith('"') && path.endsWith('"')) ||
            (path.startsWith("'") && path.endsWith("'"))) {
            path = path.slice(1, -1);
        }
        return path.trim();
    }

    /**
     * Open the host's native file picker and load whatever the user chooses.
     *
     * The dialog runs on the machine hosting the server, and blocks until it
     * is dismissed - so the button is disabled meanwhile to make that visible
     * and to avoid a second request the server would refuse anyway.
     */
    async _browseFor(type) {
        const button = document.getElementById(`${type}-browse-btn`);
        const input = type === 'video' ? this.videoPathInput : this.gpsPathInput;
        if (!input) return;

        const original = button?.textContent;
        if (button) {
            button.disabled = true;
            button.textContent = 'Choosing…';
        }

        try {
            const response = await fetch('/api/browse', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ kind: type }),
            });

            if (!response.ok) {
                const detail = await response.json().catch(() => ({}));
                throw new Error(detail.detail || `Browse failed (${response.status})`);
            }

            const { path } = await response.json();
            // A cancelled dialog returns null; leave whatever was typed alone.
            if (!path) return;

            input.value = path;
            await this._loadLocalFile(type);
        } catch (error) {
            console.error('Browse failed:', error);
            alert(error.message || 'Could not open the file browser');
        } finally {
            if (button) {
                button.disabled = false;
                button.textContent = original;
            }
        }
    }

    async _loadLocalFile(type) {
        const input = type === 'video' ? this.videoPathInput : this.gpsPathInput;
        const path = this._cleanPath(input.value);
        if (!path) return;

        // Reading a large clip off an external volume is real work and can take
        // minutes, so say so - and disable the button, since a second click
        // would queue another read of the same file.
        const loadButton = document.getElementById(`${type}-load-btn`);
        const loadButtonText = loadButton?.textContent;
        if (loadButton) {
            loadButton.disabled = true;
            loadButton.textContent = 'Reading…';
        }

        // Determine role based on current state and type
        const hasVideo = this.state.getPrimaryFile()?.file_type === 'video';
        const hasGps = this.state.getSecondaryFile() ||
                       (this.state.getPrimaryFile()?.file_type === 'gpx' ||
                        this.state.getPrimaryFile()?.file_type === 'fit' ||
                        this.state.getPrimaryFile()?.file_type === 'srt');

        const hasGpsPrimary = !hasVideo &&
            (this.state.getPrimaryFile()?.file_type === 'gpx' ||
             this.state.getPrimaryFile()?.file_type === 'fit' ||
             this.state.getPrimaryFile()?.file_type === 'srt');

        try {
            let response;

            if (type === 'video') {
                // Send session_id to reuse session when GPS is loaded (as primary or secondary)
                const body = { file_path: path };
                const hasSecondary = !!this.state.getSecondaryFile();
                if (this.state.sessionId && (hasGpsPrimary || (hasVideo && hasSecondary))) {
                    body.session_id = this.state.sessionId;
                }
                response = await fetch('/api/local-file', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body)
                });
            } else {
                // GPS file
                if (hasVideo && this.state.sessionId) {
                    // Add as secondary to existing video session
                    response = await fetch('/api/local-file-secondary', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            session_id: this.state.sessionId,
                            file_path: path
                        })
                    });
                } else {
                    // No video - GPS becomes primary (gpx-only mode)
                    response = await fetch('/api/local-file', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ file_path: path })
                    });
                }
            }

            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.detail || 'Failed to load file');
            }

            const data = await response.json();

            if (type === 'video') {
                // Video loaded (new or replaced) — always use setSession to update duration/timeline
                this.state.setSession(data.session_id, data);
            } else if (!hasVideo) {
                // GPS loaded as primary (no video) — new session
                this.state.setSession(data.session_id, data);
            } else {
                // GPS added as secondary to existing video
                this.state.setFiles(data.files);
            }

            input.value = '';

        } catch (error) {
            console.error('Load failed:', error);
            alert(error.message);
        } finally {
            if (loadButton) {
                loadButton.disabled = false;
                loadButton.textContent = loadButtonText;
            }
        }
    }

    /**
     * Show how far a file upload has got, in its own drop zone.
     *
     * @param {'video'|'gps'} type
     * @param {string} fileName
     * @param {number|null} fraction - 0..1, or null when the size is unknown
     */
    _showUploadProgress(type, fileName, fraction) {
        const zone = document.getElementById(`${type}-drop-zone`);
        if (!zone) return;  // Local mode has path inputs, not drop zones.

        let bar = zone.querySelector('.upload-progress-bar');
        if (!bar) {
            bar = document.createElement('div');
            bar.className = 'upload-progress-bar';
            bar.appendChild(document.createElement('span'));
            zone.appendChild(bar);
        }
        const percent = fraction === null ? null : Math.round(fraction * 100);
        bar.firstChild.style.width = percent === null ? '100%' : `${percent}%`;
        bar.classList.toggle('indeterminate', percent === null);

        const text = zone.querySelector('.drop-zone-text');
        if (text) {
            text.textContent = percent === null
                ? `Uploading ${fileName}…`
                : `Uploading ${fileName} — ${percent}%`;
        }
        zone.classList.add('uploading');
    }

    /** Take the upload bar back down, however the upload ended. */
    _clearUploadProgress(type) {
        const zone = document.getElementById(`${type}-drop-zone`);
        if (!zone) return;
        zone.classList.remove('uploading');
        zone.querySelector('.upload-progress-bar')?.remove();
        const text = zone.querySelector('.drop-zone-text');
        if (text) {
            text.textContent = type === 'video' ? 'Drop MP4/MOV or click' : 'Drop GPX/FIT/SRT or click';
        }
    }

    async _uploadFile(file, type) {
        const validExtensions = type === 'video' ? ['.mp4', '.mov'] : ['.gpx', '.fit', '.srt'];
        const ext = '.' + file.name.split('.').pop().toLowerCase();

        if (!validExtensions.includes(ext)) {
            alert(`Invalid file type. Expected: ${validExtensions.join(', ')}`);
            return;
        }

        const hasVideo = this.state.getPrimaryFile()?.file_type === 'video';
        const hasGpsPrimary = !hasVideo &&
            (this.state.getPrimaryFile()?.file_type === 'gpx' ||
             this.state.getPrimaryFile()?.file_type === 'fit' ||
             this.state.getPrimaryFile()?.file_type === 'srt');

        try {
            const formData = new FormData();
            formData.append('file', file);

            // A multi-GB video uploads in silence otherwise - fetch reports
            // nothing until the whole body has gone.
            const report = (fraction) => this._showUploadProgress(type, file.name, fraction);
            report(0);

            let response;

            if (type === 'video') {
                // Send session_id to reuse session when GPS is loaded (as primary or secondary)
                const hasSecondary = !!this.state.getSecondaryFile();
                if (this.state.sessionId && (hasGpsPrimary || (hasVideo && hasSecondary))) {
                    formData.append('session_id', this.state.sessionId);
                }
                response = await window.Upload.post('/api/upload', formData, report);
            } else {
                if (hasVideo && this.state.sessionId) {
                    formData.append('session_id', this.state.sessionId);
                    response = await window.Upload.post('/api/upload-secondary', formData, report);
                } else {
                    response = await window.Upload.post('/api/upload', formData, report);
                }
            }

            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.detail || 'Upload failed');
            }

            const data = await response.json();

            if (type === 'video') {
                // Video loaded (new or replaced) — always use setSession to update duration/timeline
                this.state.setSession(data.session_id, data);
            } else if (!hasVideo) {
                // GPS loaded as primary (no video) — new session
                this.state.setSession(data.session_id, data);
            } else {
                // GPS added as secondary to existing video
                this.state.setFiles(data.files);
            }

        } catch (error) {
            console.error('Upload failed:', error);
            alert(error.message);
        } finally {
            this._clearUploadProgress(type);
        }
    }

    async _clearFile(type) {
        const primary = this.state.getPrimaryFile();
        const secondary = this.state.getSecondaryFile();

        if (type === 'video') {
            if (primary?.file_type === 'video' && secondary) {
                // Video has secondary GPS - remove video only, promote GPS to primary
                try {
                    const response = await fetch(`/api/session/${this.state.sessionId}/primary`, {
                        method: 'DELETE'
                    });
                    if (response.ok) {
                        const data = await response.json();
                        this.state.setFiles(data.files);
                    } else {
                        const error = await response.json().catch(() => ({}));
                        console.error('Failed to remove video file:', error);
                        alert(error.detail || 'Failed to remove video file');
                    }
                } catch (error) {
                    console.error('Failed to remove video file:', error);
                }
            } else if (primary?.file_type === 'video') {
                // Video only, no GPS - clear entire session
                this.state.clearSession();
            }
        } else {
            // Clearing GPS
            if (secondary) {
                // Remove secondary file
                try {
                    const response = await fetch(`/api/session/${this.state.sessionId}/secondary`, {
                        method: 'DELETE'
                    });
                    if (response.ok) {
                        const data = await response.json();
                        this.state.setFiles(data.files);
                    }
                } catch (error) {
                    console.error('Failed to remove GPS file:', error);
                }
            } else if (primary?.file_type === 'gpx' || primary?.file_type === 'fit' || primary?.file_type === 'srt') {
                // GPS is primary (gpx-only mode) - clear session
                this.state.clearSession();
            }
        }
    }

    _updateUI() {
        const primary = this.state.getPrimaryFile();
        const secondary = this.state.getSecondaryFile();

        // Determine what's loaded
        const videoFile = primary?.file_type === 'video' ? primary : null;
        const gpsFile = secondary ||
                        (primary?.file_type === 'gpx' || primary?.file_type === 'fit' || primary?.file_type === 'srt' ? primary : null);

        // Update Video field
        this._updateFieldUI('video', videoFile);

        // Update GPS field
        this._updateFieldUI('gps', gpsFile);

        // Update mode indicator
        this._updateModeIndicator(videoFile, gpsFile);
    }

    _updateFieldUI(type, file) {
        const fileInfo = type === 'video' ? this.videoFileInfo : this.gpsFileInfo;
        const dropZone = type === 'video' ? this.videoDropZone : this.gpsDropZone;
        const pathInput = type === 'video' ? this.videoPathInput : this.gpsPathInput;

        if (file) {
            // Show file info
            const nameEl = fileInfo.querySelector('.file-info-name');
            nameEl.textContent = file.filename;
            fileInfo.style.display = 'flex';

            if (this.localMode && pathInput) {
                pathInput.parentElement.style.display = 'none';
            } else if (dropZone) {
                dropZone.style.display = 'none';
            }
        } else {
            // Show input
            fileInfo.style.display = 'none';

            if (this.localMode && pathInput) {
                pathInput.parentElement.style.display = 'flex';
            } else if (dropZone) {
                dropZone.style.display = 'flex';
            }
        }
    }

    _updateModeIndicator(videoFile, gpsFile) {
        if (!videoFile && !gpsFile) {
            this.modeIndicator.style.display = 'none';
            return;
        }

        this.modeIndicator.style.display = 'block';

        if (videoFile && gpsFile) {
            this.modeIndicator.innerHTML = '<span class="mode-badge mode-merge">Merge Mode</span> Video + external GPS';
        } else if (videoFile) {
            this.modeIndicator.innerHTML = '<span class="mode-badge mode-video">Video Mode</span> Using embedded GPS';
        } else if (gpsFile) {
            this.modeIndicator.innerHTML = '<span class="mode-badge mode-gps">GPS Only</span> Overlay without video';
        }
    }
}

// Export
window.FileUploader = FileUploader;
