/* PDF.js slide-out viewer controller */

var pdfViewerState = {
    doc: null,
    currentPage: 1,
    totalPages: 0,
    scale: 1.2,
    rendering: false
};

function openPdfViewer(netfileFilingId, title) {
    var panel = document.getElementById("pdf-panel");
    var titleEl = panel.querySelector(".pdf-panel-title");
    var contentEl = document.getElementById("pdf-panel-content");
    var downloadLink = document.getElementById("pdf-download-link");

    titleEl.textContent = title || "PDF Viewer";
    downloadLink.href = "/pdfs/" + netfileFilingId + ".pdf";

    // Set up viewer HTML
    contentEl.innerHTML =
        '<div class="pdf-controls">' +
            '<button class="btn btn-sm btn-ghost" onclick="pdfPrevPage()">Prev</button>' +
            '<span id="pdf-page-info">Loading...</span>' +
            '<button class="btn btn-sm btn-ghost" onclick="pdfNextPage()">Next</button>' +
            '<button class="btn btn-sm btn-ghost" onclick="pdfZoomOut()">-</button>' +
            '<button class="btn btn-sm btn-ghost" onclick="pdfZoomIn()">+</button>' +
        '</div>' +
        '<div style="text-align: center; overflow: auto; flex: 1;">' +
            '<canvas id="pdf-canvas"></canvas>' +
        '</div>';

    // Open panel
    panel.hidden = false;
    requestAnimationFrame(function() {
        panel.classList.add("open");
    });

    // Load PDF
    var url = "/pdfs/" + netfileFilingId + ".pdf";
    if (typeof pdfjsLib === "undefined") {
        contentEl.innerHTML = '<p class="text-muted" style="padding: 2rem;">PDF.js library not loaded. <a href="' + url + '" target="_blank">Download PDF</a></p>';
        return;
    }

    pdfjsLib.getDocument(url).promise.then(function(doc) {
        pdfViewerState.doc = doc;
        pdfViewerState.totalPages = doc.numPages;
        pdfViewerState.currentPage = 1;
        renderPdfPage(1);
    }).catch(function(err) {
        contentEl.innerHTML = '<p class="text-muted" style="padding: 2rem;">Failed to load PDF. <a href="' + url + '" target="_blank">Download instead</a></p>';
    });
}

function renderPdfPage(pageNum) {
    if (!pdfViewerState.doc || pdfViewerState.rendering) return;
    pdfViewerState.rendering = true;

    var pageInfo = document.getElementById("pdf-page-info");
    if (pageInfo) {
        pageInfo.textContent = "Page " + pageNum + " / " + pdfViewerState.totalPages;
    }

    pdfViewerState.doc.getPage(pageNum).then(function(page) {
        var canvas = document.getElementById("pdf-canvas");
        if (!canvas) { pdfViewerState.rendering = false; return; }

        var viewport = page.getViewport({ scale: pdfViewerState.scale });
        canvas.height = viewport.height;
        canvas.width = viewport.width;

        var ctx = canvas.getContext("2d");
        page.render({ canvasContext: ctx, viewport: viewport }).promise.then(function() {
            pdfViewerState.rendering = false;
            pdfViewerState.currentPage = pageNum;
        });
    });
}

function pdfNextPage() {
    if (pdfViewerState.currentPage < pdfViewerState.totalPages) {
        renderPdfPage(pdfViewerState.currentPage + 1);
    }
}

function pdfPrevPage() {
    if (pdfViewerState.currentPage > 1) {
        renderPdfPage(pdfViewerState.currentPage - 1);
    }
}

function pdfZoomIn() {
    pdfViewerState.scale = Math.min(pdfViewerState.scale + 0.2, 3.0);
    renderPdfPage(pdfViewerState.currentPage);
}

function pdfZoomOut() {
    pdfViewerState.scale = Math.max(pdfViewerState.scale - 0.2, 0.4);
    renderPdfPage(pdfViewerState.currentPage);
}
