/* Dismissible "N duplicate readings were auto-resolved" note (see
 * web/routes/confirm.py's `_twin_sweep_note`) — vanilla JS, no vendor
 * deps.
 *
 * Uses event delegation on `document`, same as confirm-lightbox.js, so
 * this keeps working after HTMX swaps `#confirm-queue`'s contents
 * without ever needing to be re-initialized. Dismissal is purely
 * client-side (removes the note from the DOM) — the note re-appears on
 * the next full page load as long as the last sweep's summary still says
 * it rejected rows; nothing server-side needs to track "dismissed".
 */
(function () {
  document.addEventListener("click", function (event) {
    var button = event.target.closest(".confirm-twin-sweep-note .dismiss-note");
    if (!button) {
      return;
    }
    var note = button.closest(".confirm-twin-sweep-note");
    if (note) {
      note.remove();
    }
  });
})();
