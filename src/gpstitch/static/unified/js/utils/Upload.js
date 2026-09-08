/**
 * POST a FormData with upload progress.
 *
 * `fetch` reports nothing until the whole body has been sent, so a multi-GB
 * video uploads in total silence. XMLHttpRequest still exposes `upload.progress`,
 * which is the only way to know how far along it is.
 *
 * Returns a Response-shaped object so call sites keep reading `.ok`, `.status`
 * and `await .json()` exactly as they did with fetch.
 */
class Upload {
    /**
     * @param {string} url
     * @param {FormData} formData
     * @param {(fraction: number|null) => void} [onProgress] - 0..1, or null when
     *        the total size is unknown and only "in progress" can be reported
     * @returns {Promise<{ok: boolean, status: number, json: () => Promise<any>}>}
     */
    static post(url, formData, onProgress) {
        return new Promise((resolve, reject) => {
            const request = new XMLHttpRequest();
            request.open('POST', url);

            if (onProgress) {
                request.upload.addEventListener('progress', (event) => {
                    onProgress(event.lengthComputable ? event.loaded / event.total : null);
                });
            }

            request.addEventListener('load', () => {
                const body = request.responseText;
                resolve({
                    ok: request.status >= 200 && request.status < 300,
                    status: request.status,
                    json: async () => JSON.parse(body),
                });
            });
            request.addEventListener('error', () => reject(new Error('Upload failed')));
            request.addEventListener('abort', () => reject(new Error('Upload cancelled')));

            request.send(formData);
        });
    }
}

window.Upload = Upload;
