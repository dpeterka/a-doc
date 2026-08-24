/* Confirm-queue image lightbox — vanilla JS, no vendor deps.
 *
 * Click any source-page thumbnail (`.confirm-page-image`) to open the
 * full-resolution PNG in a modal. Click the modal image to toggle
 * fit-width <-> full-resolution-with-pan (pinch/scroll zoom on mobile
 * comes for free from the browser once the image overflows its
 * scrollable viewport — no gesture handling needed). Esc, the close
 * button, or a click on the backdrop closes it.
 *
 * Uses event delegation on `document` so it keeps working after HTMX
 * swaps `#confirm-queue`'s contents (confirm/reject/correct/bulk-confirm
 * actions all re-render that subtree; this script never has to be
 * re-initialized for it).
 */
(function () {
  "use strict";

  function init() {
    var lightbox = document.getElementById("confirm-lightbox");
    var lightboxImg = document.getElementById("confirm-lightbox-img");
    if (!lightbox || !lightboxImg) {
      return;
    }

    function openLightbox(src, alt) {
      lightboxImg.src = src;
      lightboxImg.alt = alt || "Full-resolution source page";
      lightboxImg.classList.add("fit-width");
      lightboxImg.classList.remove("full-res");
      lightbox.hidden = false;
      lightbox.classList.add("open");
      document.body.classList.add("lightbox-active");
    }

    function closeLightbox() {
      lightbox.classList.remove("open");
      lightbox.hidden = true;
      lightboxImg.src = "";
      document.body.classList.remove("lightbox-active");
    }

    document.addEventListener("click", function (event) {
      var target = event.target;
      if (!(target instanceof Element)) {
        return;
      }
      var thumb = target.closest(".confirm-page-image");
      if (thumb) {
        openLightbox(thumb.getAttribute("src") || "", thumb.getAttribute("alt"));
        return;
      }
      if (target.closest(".lightbox-close")) {
        closeLightbox();
        return;
      }
      if (target === lightboxImg) {
        lightboxImg.classList.toggle("fit-width");
        lightboxImg.classList.toggle("full-res");
        return;
      }
      if (target === lightbox) {
        closeLightbox();
      }
    });

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && lightbox.classList.contains("open")) {
        closeLightbox();
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
