/**
 * OutputFolder - where rendered videos are written.
 *
 * One setting shared by single and batch render, so the two can't disagree.
 * Unset means "alongside the source video", which is what GPStitch has always
 * done - choosing a folder is opt-in.
 *
 * Only the folder is stored: the filename is derived by the backend, because
 * the extension depends on the ffmpeg profile.
 */
const OUTPUT_FOLDER_KEY = 'gpstitch.outputFolder';
const OUTPUT_FOLDER_DEFAULT_LABEL = 'Alongside source video';

const OutputFolder = {
    /** Chosen folder, or null for the default. */
    get() {
        try {
            return localStorage.getItem(OUTPUT_FOLDER_KEY) || null;
        } catch (e) {
            // Private windows and blocked site data both throw here.
            return null;
        }
    },

    set(path) {
        try {
            localStorage.setItem(OUTPUT_FOLDER_KEY, path);
        } catch (e) {
            console.warn('Could not remember the output folder:', e);
        }
        this._notify();
    },

    clear() {
        try {
            localStorage.removeItem(OUTPUT_FOLDER_KEY);
        } catch (e) {
            console.warn('Could not clear the output folder:', e);
        }
        this._notify();
    },

    /** Text for display; never empty. */
    label() {
        return this.get() || OUTPUT_FOLDER_DEFAULT_LABEL;
    },

    /** Subscribe to changes, so every panel showing the folder stays in step. */
    onChange(handler) {
        this._handlers.push(handler);
    },

    _handlers: [],

    _notify() {
        for (const handler of this._handlers) {
            try {
                handler(this.get());
            } catch (e) {
                console.error('Output folder listener failed:', e);
            }
        }
    },

    /**
     * Ask the host for a folder and remember it.
     * Returns the chosen path, or null if the dialog was dismissed.
     */
    async choose() {
        const response = await fetch('/api/browse', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ kind: 'folder' }),
        });
        if (!response.ok) {
            const detail = await response.json().catch(() => ({}));
            throw new Error(detail.detail || `Could not open the folder browser (${response.status})`);
        }
        const { path } = await response.json();
        // Dismissed: keep whatever was already chosen.
        if (!path) return null;
        this.set(path);
        return path;
    },
};

window.OutputFolder = OutputFolder;
