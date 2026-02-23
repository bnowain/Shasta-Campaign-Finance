/* NetFile Tracker — Minimal HTMX config and UI helpers */

// HTMX configuration
document.body.addEventListener("htmx:configRequest", function(evt) {
    // Add common headers if needed
});

// Sidebar toggle
function toggleSidebar() {
    var sidebar = document.getElementById("sidebar");
    sidebar.classList.toggle("collapsed");
}

// PDF panel
function openPdfPanel() {
    var panel = document.getElementById("pdf-panel");
    panel.hidden = false;
    requestAnimationFrame(function() {
        panel.classList.add("open");
    });
}

function closePdfPanel() {
    var panel = document.getElementById("pdf-panel");
    panel.classList.remove("open");
    panel.addEventListener("transitionend", function handler() {
        panel.hidden = true;
        panel.removeEventListener("transitionend", handler);
    });
}

// Close PDF panel with Escape key
document.addEventListener("keydown", function(e) {
    if (e.key === "Escape") {
        var panel = document.getElementById("pdf-panel");
        if (panel && panel.classList.contains("open")) {
            closePdfPanel();
        }
    }
});

// Close active EventSource connections on page unload
window.addEventListener("beforeunload", function() {
    if (window._pullEventSource) {
        window._pullEventSource.close();
        window._pullEventSource = null;
    }
});

// ─── People Merge ──────────────────────────────────────

function updateMergeButton() {
    var checkboxes = document.querySelectorAll(".merge-checkbox:checked");
    var mergeBar = document.getElementById("merge-bar");
    var mergeBtn = document.getElementById("merge-btn");

    if (mergeBar) {
        if (checkboxes.length >= 2) {
            mergeBar.hidden = false;
            mergeBtn.disabled = false;
        } else if (checkboxes.length === 1) {
            mergeBar.hidden = false;
            mergeBtn.disabled = true;
        } else {
            mergeBar.hidden = true;
        }
    }
}

function openMergePreview() {
    var checkboxes = document.querySelectorAll(".merge-checkbox:checked");
    var ids = Array.from(checkboxes).map(function(cb) { return cb.value; }).join(",");

    if (ids) {
        htmx.ajax("GET", "/people/merge-preview?ids=" + ids, {
            target: "#merge-preview-area",
            swap: "innerHTML"
        });
    }
}
