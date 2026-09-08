/**
 * Show that a button's work is under way.
 *
 * Several actions here take a long time - Get Command reads a whole GPS stream,
 * batch pre-check runs ffprobe per file - and a button that looks idle invites a
 * second click that queues the work twice.
 *
 * Uses the styling the app already has: `.btn.loading` reveals a `.spinner-small`
 * child. Most buttons have no such child in their markup, so one is created on
 * first use rather than requiring every button to be edited.
 */
class Busy {
    /**
     * Run an async action with the button showing a spinner and disabled.
     *
     * The button is always restored, including when the action throws, because a
     * button stuck disabled is worse than no indicator at all. Errors propagate;
     * this only owns the button's appearance.
     *
     * @param {HTMLElement|null} button - the control to mark busy; null is a no-op wrapper
     * @param {Function} action - async function to run
     * @param {{label?: string}} [options] - label to show instead of the button's own text
     * @returns {Promise<*>} whatever the action returns
     */
    static async run(button, action, { label } = {}) {
        if (!button) {
            return action();
        }
        if (button.dataset.busy === 'true') {
            return undefined;  // Already running; ignore the repeat click.
        }

        const originalHTML = button.innerHTML;
        const wasDisabled = button.disabled;

        button.dataset.busy = 'true';
        button.disabled = true;
        button.classList.add('loading');
        if (label !== undefined) {
            button.innerHTML = '';
            const text = document.createElement('span');
            text.className = 'btn-text';
            text.textContent = label;
            button.appendChild(text);
        }
        if (!button.querySelector('.spinner-small')) {
            const spinner = document.createElement('span');
            spinner.className = 'spinner-small';
            button.appendChild(spinner);
        }

        try {
            return await action();
        } finally {
            button.classList.remove('loading');
            button.disabled = wasDisabled;
            button.innerHTML = originalHTML;
            delete button.dataset.busy;
        }
    }

    /**
     * Attach a busy-wrapped click handler.
     *
     * @param {HTMLElement|null} button
     * @param {Function} handler - async function run on click
     * @param {{label?: string}} [options]
     */
    static onClick(button, handler, options = {}) {
        button?.addEventListener('click', () => Busy.run(button, handler, options));
    }
}

window.Busy = Busy;
