/* Research Assistant integration for the vendored PDF.js generic viewer.
 * This file is injected into MainWorld by PdfReaderWindow. Keep viewer.mjs
 * untouched so upgrading the upstream distribution remains mechanical.
 */
(() => {
  "use strict";

  if (window.__researchAssistantReaderInstalled) {
    return;
  }
  window.__researchAssistantReaderInstalled = true;

  const COLORS = ["yellow", "blue", "green", "red", "orange", "purple"];
  const MAX_SELECTION_LENGTH = 50000;
  const ICON_ROOT = "research-assistant://app/icons";
  const state = {
    application: null,
    bridge: null,
    bridgeConnecting: false,
    applicationConnected: false,
    documentFingerprint: "",
    frozenSelection: null,
    currentColor: "yellow",
    highlights: new Map(),
    notes: new Map(),
    toolbar: null,
    colorMenu: null,
    toast: null,
    repaintTimer: 0,
    pendingAction: null,
    transientSelection: null,
    sourceHighlight: null,
    noteHitRegions: new Map(),
    cancelledRequests: new Set(),
  };

  const parseJson = raw => {
    if (typeof raw !== "string") {
      return null;
    }
    try {
      return JSON.parse(raw);
    } catch {
      return null;
    }
  };

  const requestId = () => {
    if (globalThis.crypto?.randomUUID) {
      return globalThis.crypto.randomUUID();
    }
    return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  };

  const showToast = (message, isError = false) => {
    if (!message) {
      return;
    }
    if (!state.toast) {
      const toast = document.createElement("div");
      toast.className = "ra-toast";
      toast.setAttribute("role", "status");
      toast.hidden = true;
      document.body.append(toast);
      state.toast = toast;
    }
    state.toast.textContent = message;
    state.toast.classList.toggle("ra-error", isError);
    state.toast.hidden = false;
    clearTimeout(state.toast._raTimer);
    state.toast._raTimer = setTimeout(() => {
      state.toast.hidden = true;
    }, 2600);
  };

  const hideToolbar = () => {
    if (state.toolbar) {
      state.toolbar.hidden = true;
    }
    if (state.colorMenu) {
      state.colorMenu.hidden = true;
    }
  };

  const clearTransientSelection = (requestId = "", cancelled = false, notify = false) => {
    const transient = state.transientSelection;
    if (!transient || (requestId && transient.requestId !== requestId)) {
      return false;
    }
    if (cancelled && transient.requestId) {
      state.cancelledRequests.add(transient.requestId);
    }
    if (state.pendingAction?.requestId === transient.requestId) {
      state.pendingAction = null;
      setToolbarPending(false);
    }
    state.transientSelection = null;
    if (notify && transient.requestId) {
      callBridge("cancelTransientSelection", transient.requestId, false);
    }
    scheduleRepaint();
    return true;
  };

  const clearSelection = () => {
    window.getSelection()?.removeAllRanges();
    state.frozenSelection = null;
    hideToolbar();
    clearTransientSelection();
  };

  const textItemsForLayer = layer =>
    Array.from(layer.querySelectorAll("span")).filter(span => {
      if (span.classList.contains("highlight") || span.classList.contains("markedContent")) {
        return false;
      }
      if (span.closest(".ra-highlight-layer")) {
        return false;
      }
      return span.closest(".textLayer") === layer && !span.closest("span.highlight");
    });

  const clearSourceHighlight = () => {
    document.querySelectorAll(".ra-source-highlight-layer").forEach(layer => layer.remove());
    state.sourceHighlight = null;
  };

  const pointerInsideSourceHighlight = event =>
    Array.from(document.querySelectorAll(".ra-source-evidence-rect")).some(rect => {
      const box = rect.getBoundingClientRect();
      return event.clientX >= box.left && event.clientX <= box.right &&
        event.clientY >= box.top && event.clientY <= box.bottom;
    });

  const normalizedSearchText = (characters, points = null) => {
    const output = [];
    const outputPoints = [];
    for (let index = 0; index < characters.length; index += 1) {
      const character = characters[index];
      if (character === "\u00ad") continue;
      const normalized = character.normalize("NFKC").toLocaleLowerCase();
      for (const value of normalized) {
        if (/\s/u.test(value)) {
          if (!output.length || output[output.length - 1] === " ") continue;
          output.push(" ");
        } else {
          output.push(value);
        }
        if (points) outputPoints.push(points[index]);
      }
    }
    const compact = [];
    const compactPoints = [];
    for (let index = 0; index < output.length; index += 1) {
      const hyphenated = output[index] === "-" && output[index + 1] === " " &&
        /[\p{L}\p{N}]/u.test(output[index - 1] || "") &&
        /[\p{L}\p{N}]/u.test(output[index + 2] || "");
      if (hyphenated) {
        index += 1;
        continue;
      }
      compact.push(output[index]);
      if (points) compactPoints.push(outputPoints[index]);
    }
    let start = 0;
    let end = compact.length;
    while (start < end && compact[start] === " ") start += 1;
    while (end > start && compact[end - 1] === " ") end -= 1;
    return {
      text: compact.slice(start, end).join(""),
      points: points ? compactPoints.slice(start, end) : [],
    };
  };

  const searchableTextLayer = textLayer => {
    const characters = [];
    const points = [];
    const items = textItemsForLayer(textLayer);
    for (const item of items) {
      const walker = document.createTreeWalker(item, NodeFilter.SHOW_TEXT);
      let node = walker.nextNode();
      while (node) {
        for (let offset = 0; offset < node.data.length; offset += 1) {
          characters.push(node.data[offset]);
          points.push({ node, offset });
        }
        node = walker.nextNode();
      }
      const tail = points[points.length - 1];
      if (tail) {
        characters.push(" ");
        points.push({ node: tail.node, offset: tail.node.data.length });
      }
    }
    return normalizedSearchText(characters, points);
  };

  const evidenceVariants = evidence => {
    const normalized = normalizedSearchText(Array.from(evidence)).text;
    const words = normalized.split(" ").filter(Boolean);
    const values = [normalized];
    for (const size of [16, 12, 8]) {
      if (words.length <= size) continue;
      values.push(words.slice(0, size).join(" "));
      values.push(words.slice(-size).join(" "));
      const start = Math.max(0, Math.floor((words.length - size) / 2));
      values.push(words.slice(start, start + size).join(" "));
    }
    return [...new Set(values.filter(value => value.length >= 18))];
  };

  const sourceRange = (textLayer, evidence) => {
    const searchable = searchableTextLayer(textLayer);
    for (const candidate of evidenceVariants(evidence)) {
      const start = searchable.text.indexOf(candidate);
      if (start < 0) continue;
      const first = searchable.points[start];
      const last = searchable.points[start + candidate.length - 1];
      if (!first?.node || !last?.node) continue;
      const range = document.createRange();
      range.setStart(first.node, Math.min(first.offset, first.node.data.length));
      range.setEnd(last.node, Math.min(last.offset + 1, last.node.data.length));
      return range;
    }
    return null;
  };

  const renderSourceEvidence = (pageNumber, evidence) => {
    const page = document.querySelector(`.page[data-page-number="${pageNumber}"]`);
    const textLayer = page?.querySelector(".textLayer");
    if (!page || !textLayer) return false;
    const range = sourceRange(textLayer, evidence);
    if (!range) return false;
    document.querySelectorAll(".ra-source-highlight-layer").forEach(layer => layer.remove());
    const layer = document.createElement("div");
    layer.className = "ra-source-highlight-layer";
    layer.setAttribute("aria-hidden", "true");
    const layerRect = textLayer.getBoundingClientRect();
    for (const rect of Array.from(range.getClientRects())) {
      if (rect.width < 0.5 || rect.height < 0.5) continue;
      const marker = document.createElement("div");
      marker.className = "ra-source-evidence-rect";
      marker.style.left = `${rect.left - layerRect.left}px`;
      marker.style.top = `${rect.top - layerRect.top}px`;
      marker.style.width = `${rect.width}px`;
      marker.style.height = `${rect.height}px`;
      layer.append(marker);
    }
    if (!layer.childElementCount) return false;
    textLayer.insertBefore(layer, textLayer.firstChild);
    layer.firstElementChild?.scrollIntoView({ block: "center", behavior: "smooth" });
    return true;
  };

  const showSourceEvidence = payload => {
    const page = Number(payload?.page);
    const evidence = typeof payload?.evidence === "string" ? payload.evidence.trim() : "";
    if (!Number.isInteger(page) || page < 1 || !evidence) return false;
    clearSourceHighlight();
    state.sourceHighlight = { page, evidence };
    state.application?.pdfLinkService?.goToPage(page);
    let attempts = 0;
    const reveal = () => {
      if (!state.sourceHighlight || state.sourceHighlight.page !== page) return;
      attempts += 1;
      if (renderSourceEvidence(page, evidence)) return;
      if (attempts < 30) {
        setTimeout(reveal, 100);
      } else {
        showToast("Source page opened; exact passage could not be highlighted.");
      }
    };
    setTimeout(reveal, 0);
    return true;
  };

  const boundaryInItem = (container, offset, items) => {
    const itemIndex = items.findIndex(item => item === container || item.contains(container));
    if (itemIndex < 0) {
      return null;
    }
    const item = items[itemIndex];
    const prefixRange = document.createRange();
    prefixRange.selectNodeContents(item);
    try {
      prefixRange.setEnd(container, offset);
    } catch {
      return null;
    }
    return {
      item: itemIndex,
      offset: prefixRange.toString().length,
    };
  };

  const clippedClientRects = (range, canvasRect) => {
    const result = [];
    for (const rect of range.getClientRects()) {
      const left = Math.max(rect.left, canvasRect.left);
      const top = Math.max(rect.top, canvasRect.top);
      const right = Math.min(rect.right, canvasRect.right);
      const bottom = Math.min(rect.bottom, canvasRect.bottom);
      if (right - left > 0.5 && bottom - top > 0.5) {
        result.push({ left, top, right, bottom });
      }
    }
    return result;
  };

  const captureSelection = selection => {
    if (!selection || selection.rangeCount !== 1 || selection.isCollapsed) {
      return null;
    }
    const rawText = selection.toString();
    if (!rawText.trim() || rawText.length > MAX_SELECTION_LENGTH) {
      return null;
    }

    const range = selection.getRangeAt(0).cloneRange();
    const startElement = range.startContainer.nodeType === Node.ELEMENT_NODE
      ? range.startContainer
      : range.startContainer.parentElement;
    const endElement = range.endContainer.nodeType === Node.ELEMENT_NODE
      ? range.endContainer
      : range.endContainer.parentElement;
    const startPage = startElement?.closest?.(".page[data-page-number]");
    const endPage = endElement?.closest?.(".page[data-page-number]");
    if (!startPage || !endPage) {
      return null;
    }

    const clientRects = Array.from(range.getClientRects()).filter(
      rect => rect.width > 0.5 && rect.height > 0.5
    );
    if (!clientRects.length) {
      return null;
    }

    const frozen = {
      requestId: requestId(),
      selectedText: rawText,
      location: null,
      range,
      toolbarRect: clientRects.reduce(
        (box, rect) => ({
          left: Math.min(box.left, rect.left),
          top: Math.min(box.top, rect.top),
          right: Math.max(box.right, rect.right),
          bottom: Math.max(box.bottom, rect.bottom),
        }),
        {
          left: clientRects[0].left,
          top: clientRects[0].top,
          right: clientRects[0].right,
          bottom: clientRects[0].bottom,
        }
      ),
    };

    // Persisted annotations and translation intentionally stay on one page.
    // Cross-page copy can still use the frozen selection text.
    if (startPage !== endPage) {
      return frozen;
    }

    const pageNumber = Number(startPage.dataset.pageNumber);
    const pageView = state.application?.pdfViewer?.getPageView(pageNumber - 1);
    const textLayer = startPage.querySelector(".textLayer");
    const canvas = startPage.querySelector(".canvasWrapper") || startPage;
    if (!pageView?.viewport || !textLayer || !canvas) {
      return frozen;
    }

    const items = textItemsForLayer(textLayer);
    const start = boundaryInItem(range.startContainer, range.startOffset, items);
    const end = boundaryInItem(range.endContainer, range.endOffset, items);
    if (!start || !end) {
      return frozen;
    }

    const itemText = items.map(item => item.textContent || "");
    const itemStarts = [];
    let textLength = 0;
    for (const text of itemText) {
      itemStarts.push(textLength);
      textLength += text.length;
    }
    const absoluteStart = itemStarts[start.item] + start.offset;
    const absoluteEnd = itemStarts[end.item] + end.offset;
    const pageText = itemText.join("");
    const canvasRect = canvas.getBoundingClientRect();
    const pdfRects = clippedClientRects(range, canvasRect).map(rect => {
      const first = pageView.viewport.convertToPdfPoint(
        rect.left - canvasRect.left,
        rect.top - canvasRect.top
      );
      const second = pageView.viewport.convertToPdfPoint(
        rect.right - canvasRect.left,
        rect.bottom - canvasRect.top
      );
      return [first[0], first[1], second[0], second[1]];
    });
    if (!pdfRects.length) {
      return frozen;
    }

    frozen.location = {
      version: 1,
      fingerprint: state.documentFingerprint,
      segments: [
        {
          page: pageNumber,
          start,
          end,
          exact: pageText.slice(absoluteStart, absoluteEnd),
          prefix: pageText.slice(Math.max(0, absoluteStart - 64), absoluteStart),
          suffix: pageText.slice(absoluteEnd, absoluteEnd + 64),
          pdfRects,
        },
      ],
    };
    return frozen;
  };

  const payloadForAction = color => {
    if (!state.frozenSelection) {
      return null;
    }
    return {
      requestId: requestId(),
      selectedText: state.frozenSelection.selectedText,
      location: state.frozenSelection.location,
      ...(color ? { color } : {}),
    };
  };

  const requirePageLocation = () => {
    if (state.frozenSelection?.location) {
      return true;
    }
    showToast("Highlight and Note require a selection within one rendered page.", true);
    return false;
  };

  const callBridge = (method, payload, serialize = true) => {
    if (!state.bridge || typeof state.bridge[method] !== "function") {
      showToast("Reader actions are still connecting. Try again.", true);
      return false;
    }
    try {
      state.bridge[method](serialize ? JSON.stringify(payload) : payload);
    } catch {
      showToast("Reader action could not be sent. Try again.", true);
      return false;
    }
    return true;
  };

  const iconNode = name => {
    const icon = document.createElement("span");
    icon.className = "ra-icon";
    icon.setAttribute("aria-hidden", "true");
    icon.style.setProperty("--ra-icon", `url("${ICON_ROOT}/${name}.svg")`);
    return icon;
  };

  const setToolbarPending = pending => {
    state.toolbar?.classList.toggle("ra-pending", pending);
    for (const button of state.toolbar?.querySelectorAll("button") || []) {
      button.disabled = pending;
    }
  };

  const beginPendingAction = (action, payload, { transient = "" } = {}) => {
    if (state.pendingAction || !payload) {
      return false;
    }
    if (!state.bridge || typeof state.bridge[action] !== "function") {
      showToast("Reader actions are still connecting. Try again.", true);
      return false;
    }
    state.pendingAction = {
      action,
      requestId: payload.requestId,
      payload,
    };
    setToolbarPending(true);

    if (transient) {
      state.transientSelection = {
        requestId: payload.requestId,
        location: payload.location,
        kind: transient,
      };
      window.getSelection()?.removeAllRanges();
      state.frozenSelection = null;
      hideToolbar();
      scheduleRepaint();
    }

    if (!callBridge(action, payload)) {
      state.pendingAction = null;
      setToolbarPending(false);
      if (transient) {
        clearTransientSelection(payload.requestId);
      }
      return false;
    }
    return true;
  };

  const createToolbarButton = (label, iconName, action, className = "") => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `ra-selection-action ${className}`.trim();
    button.title = label;
    button.setAttribute("aria-label", label);
    button.append(iconNode(iconName));
    const text = document.createElement("span");
    text.className = "ra-action-label";
    text.textContent = label;
    button.append(text);
    button.addEventListener("pointerdown", event => event.preventDefault());
    button.addEventListener("click", action);
    return button;
  };

  const createSelectionToolbar = () => {
    if (state.toolbar || !document.body) {
      return;
    }
    const toolbar = document.createElement("div");
    toolbar.className = "ra-selection-toolbar";
    toolbar.setAttribute("role", "toolbar");
    toolbar.setAttribute("aria-label", "Selected text actions");
    toolbar.hidden = true;
    toolbar.addEventListener("pointerdown", event => event.preventDefault());

    toolbar.append(
      createToolbarButton("Highlight", "highlighter", () => {
        if (!requirePageLocation()) return;
        beginPendingAction(
          "createHighlight",
          payloadForAction(state.currentColor)
        );
        state.colorMenu.hidden = true;
      }, "ra-highlight-action")
    );

    const colorToggle = createToolbarButton("Highlight color", "palette", () => {
      state.colorMenu.hidden = !state.colorMenu.hidden;
    }, "ra-color-toggle ra-icon-only");
    colorToggle.setAttribute("aria-label", "Choose highlight color");
    colorToggle.dataset.color = state.currentColor;
    toolbar.append(colorToggle);

    const colorMenu = document.createElement("div");
    colorMenu.className = "ra-color-menu";
    colorMenu.hidden = true;
    for (const color of COLORS) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "ra-color-choice";
      button.dataset.color = color;
      button.title = color[0].toUpperCase() + color.slice(1);
      button.setAttribute("aria-label", `Use ${color} highlight`);
      button.addEventListener("pointerdown", event => event.preventDefault());
      button.addEventListener("click", () => {
        state.currentColor = color;
        colorToggle.dataset.color = color;
        colorMenu.hidden = true;
        if (!requirePageLocation()) return;
        beginPendingAction("createHighlight", payloadForAction(color));
      });
      colorMenu.append(button);
    }
    toolbar.append(colorMenu);
    state.colorMenu = colorMenu;

    toolbar.append(
      createToolbarButton("Note", "message-square", () => {
        if (!requirePageLocation()) return;
        beginPendingAction("createSelectionNote", payloadForAction(), {
          transient: "note",
        });
      }),
      createToolbarButton("Translate", "languages", () => {
        if (!state.frozenSelection?.location) {
          showToast("Translate requires a selection within one rendered page.", true);
          return;
        }
        beginPendingAction("translateSelection", payloadForAction(), {
          transient: "translation",
        });
      }),
      createToolbarButton("Ask AI", "sparkles", () => {
        if (!requirePageLocation()) return;
        beginPendingAction("askAISelection", payloadForAction());
      }),
      createToolbarButton("Copy", "copy", () => {
        if (!state.frozenSelection?.location) {
          // Cross-page copy remains available through the native selection.
          const text = state.frozenSelection?.selectedText || "";
          if (!navigator.clipboard?.writeText) {
            showToast("Clipboard access is unavailable.", true);
            return;
          }
          navigator.clipboard.writeText(text).then(() => {
            showToast("Copied");
            clearSelection();
          }).catch(() => {
            showToast("Could not copy the selection.", true);
          });
          return;
        }
        beginPendingAction("copySelection", payloadForAction());
      })
    );

    document.body.append(toolbar);
    state.toolbar = toolbar;
  };

  const currentToolbarRect = frozen => {
    const range = frozen?.range;
    if (!range) {
      return frozen?.toolbarRect || null;
    }
    const container = range.commonAncestorContainer;
    const element = container?.nodeType === Node.ELEMENT_NODE
      ? container
      : container?.parentElement;
    if (!element?.isConnected) {
      return null;
    }
    try {
      const rects = Array.from(range.getClientRects()).filter(
        rect => rect.width > 0.5 && rect.height > 0.5
      );
      if (!rects.length) {
        return frozen.toolbarRect;
      }
      return rects.reduce(
        (box, rect) => ({
          left: Math.min(box.left, rect.left),
          top: Math.min(box.top, rect.top),
          right: Math.max(box.right, rect.right),
          bottom: Math.max(box.bottom, rect.bottom),
        }),
        {
          left: rects[0].left,
          top: rects[0].top,
          right: rects[0].right,
          bottom: rects[0].bottom,
        }
      );
    } catch {
      return null;
    }
  };

  const positionToolbar = frozen => {
    createSelectionToolbar();
    if (!state.toolbar || !frozen) {
      hideToolbar();
      return;
    }
    const rect = currentToolbarRect(frozen);
    if (!rect) {
      hideToolbar();
      return;
    }
    frozen.toolbarRect = rect;
    state.toolbar.hidden = false;
    state.colorMenu.hidden = true;
    const width = state.toolbar.offsetWidth;
    const height = state.toolbar.offsetHeight;
    const left = Math.max(8, Math.min(window.innerWidth - width - 8, (rect.left + rect.right - width) / 2));
    const above = rect.top - height - 10;
    const top = above >= 8 ? above : Math.min(window.innerHeight - height - 8, rect.bottom + 10);
    state.toolbar.style.left = `${left}px`;
    state.toolbar.style.top = `${top}px`;
  };

  const refreshSelectionToolbar = () => {
    const toolbarSelection = state.toolbar?.contains(document.activeElement);
    if (toolbarSelection) {
      return;
    }
    const frozen = captureSelection(window.getSelection());
    if (!frozen) {
      hideToolbar();
      return;
    }
    if (state.transientSelection) {
      clearTransientSelection(
        state.transientSelection.requestId,
        true,
        true
      );
    }
    state.frozenSelection = frozen;
    positionToolbar(frozen);
  };

  const layersForPage = page => {
    const textLayer = page.querySelector(".textLayer");
    if (!textLayer) {
      return null;
    }
    textLayer.querySelector(":scope > .ra-highlight-layer")?.remove();

    const visualLayer = document.createElement("div");
    visualLayer.className = "ra-highlight-layer";
    visualLayer.setAttribute("aria-hidden", "true");
    textLayer.insertBefore(visualLayer, textLayer.firstChild);
    return visualLayer;
  };

  const viewportBox = (pageView, pdfRect) => {
    if (!Array.isArray(pdfRect) || pdfRect.length !== 4) {
      return null;
    }
    const viewportRect = pageView.viewport.convertToViewportRectangle(pdfRect);
    const left = Math.min(viewportRect[0], viewportRect[2]);
    const top = Math.min(viewportRect[1], viewportRect[3]);
    const width = Math.abs(viewportRect[2] - viewportRect[0]);
    const height = Math.abs(viewportRect[3] - viewportRect[1]);
    if (![left, top, width, height].every(Number.isFinite) || width <= 0 || height <= 0) {
      return null;
    }
    return { left, top, width, height, right: left + width, bottom: top + height };
  };

  const positionRectangle = (rectangle, box) => {
    rectangle.style.left = `${box.left}px`;
    rectangle.style.top = `${box.top}px`;
    rectangle.style.width = `${box.width}px`;
    rectangle.style.height = `${box.height}px`;
  };

  const pageBoxesForLocation = (location, pageNumber, pageView) => {
    if (!location || location.version !== 1) {
      return [];
    }
    if (location.fingerprint && location.fingerprint !== state.documentFingerprint) {
      return [];
    }
    const boxes = [];
    for (const segment of location.segments || []) {
      if (segment.page !== pageNumber) {
        continue;
      }
      for (const pdfRect of segment.pdfRects || []) {
        const box = viewportBox(pageView, pdfRect);
        if (box) {
          boxes.push(box);
        }
      }
    }
    return boxes;
  };

  const renderPageAnnotations = pageNumber => {
    const application = state.application;
    const page = document.querySelector(`.page[data-page-number="${pageNumber}"]`);
    const pageView = application?.pdfViewer?.getPageView(pageNumber - 1);
    if (!page || !pageView?.viewport) {
      return;
    }
    const visualLayer = layersForPage(page);
    if (!visualLayer) {
      return;
    }
    const linkedNoteIds = new Set();
    const pageHitRegions = [];

    for (const highlight of state.highlights.values()) {
      const boxes = pageBoxesForLocation(highlight.location, pageNumber, pageView);
      for (const box of boxes) {
        const rectangle = document.createElement("div");
        rectangle.className = "ra-highlight-rect";
        rectangle.dataset.highlightId = String(highlight.id);
        rectangle.dataset.color = COLORS.includes(highlight.color)
          ? highlight.color
          : "yellow";
        positionRectangle(rectangle, box);
        visualLayer.append(rectangle);
      }
      if (Number.isInteger(highlight.noteId) && highlight.noteId > 0) {
        linkedNoteIds.add(highlight.noteId);
        if (boxes.length) {
          for (const box of boxes) {
            const underline = document.createElement("div");
            underline.className = "ra-note-underline";
            underline.dataset.noteId = String(highlight.noteId);
            positionRectangle(underline, box);
            visualLayer.append(underline);
            pageHitRegions.push({ noteId: highlight.noteId, box });
          }
        }
      }
    }

    for (const note of state.notes.values()) {
      if (linkedNoteIds.has(note.id)) {
        continue;
      }
      const boxes = pageBoxesForLocation(note.location, pageNumber, pageView);
      for (const box of boxes) {
        const noteRect = document.createElement("div");
        noteRect.className = "ra-note-rect ra-note-underline";
        noteRect.dataset.noteId = String(note.id);
        positionRectangle(noteRect, box);
        visualLayer.append(noteRect);
        pageHitRegions.push({ noteId: note.id, box });
      }
    }

    const transientBoxes = pageBoxesForLocation(
      state.transientSelection?.location,
      pageNumber,
      pageView
    );
    for (const box of transientBoxes) {
      const rectangle = document.createElement("div");
      rectangle.className = state.transientSelection?.kind === "note"
        ? "ra-note-draft-selection-rect"
        : "ra-translation-selection-rect";
      positionRectangle(rectangle, box);
      visualLayer.append(rectangle);
    }
    state.noteHitRegions.set(pageNumber, pageHitRegions);
  };

  const scheduleRepaint = () => {
    clearTimeout(state.repaintTimer);
    state.repaintTimer = setTimeout(() => {
      for (const page of document.querySelectorAll(".page[data-page-number]")) {
        if (page.querySelector(".textLayer")) {
          renderPageAnnotations(Number(page.dataset.pageNumber));
        }
      }
    }, 50);
  };

  const setHighlights = raw => {
    const records = parseJson(raw);
    if (!Array.isArray(records)) {
      return;
    }
    state.highlights.clear();
    for (const record of records) {
      if (record && Number.isInteger(record.id)) {
        state.highlights.set(record.id, record);
      }
    }
    scheduleRepaint();
  };

  const setNotes = raw => {
    const records = parseJson(raw);
    if (!Array.isArray(records)) {
      return;
    }
    state.notes.clear();
    for (const record of records) {
      if (record && Number.isInteger(record.id) && record.location?.version === 1) {
        state.notes.set(record.id, record);
      }
    }
    scheduleRepaint();
  };

  const upsertHighlight = raw => {
    const record = parseJson(raw);
    if (!record || !Number.isInteger(record.id)) {
      return;
    }
    state.highlights.set(record.id, record);
    scheduleRepaint();
  };

  const removeHighlight = highlightId => {
    state.highlights.delete(Number(highlightId));
    scheduleRepaint();
  };

  const navigateToLocation = raw => {
    const location = parseJson(raw);
    const segment = location?.segments?.[0];
    if (!segment || !Number.isInteger(segment.page)) {
      return;
    }
    const application = state.application;
    application?.pdfLinkService?.goToPage(segment.page);

    let attempts = 0;
    const reveal = () => {
      attempts += 1;
      const page = document.querySelector(`.page[data-page-number="${segment.page}"]`);
      const pageView = application?.pdfViewer?.getPageView(segment.page - 1);
      const textLayer = page?.querySelector(".textLayer");
      if (!page || !pageView?.viewport || !textLayer) {
        if (attempts < 30) setTimeout(reveal, 100);
        else page?.scrollIntoView({ block: "center", behavior: "smooth" });
        return;
      }

      const firstRect = segment.pdfRects?.[0];
      if (!Array.isArray(firstRect) || firstRect.length !== 4) {
        page.scrollIntoView({ block: "center", behavior: "smooth" });
        return;
      }
      const viewportRect = pageView.viewport.convertToViewportRectangle(firstRect);
      const pulse = document.createElement("div");
      pulse.className = "ra-navigation-pulse";
      pulse.style.left = `${Math.min(viewportRect[0], viewportRect[2])}px`;
      pulse.style.top = `${Math.min(viewportRect[1], viewportRect[3])}px`;
      pulse.style.width = `${Math.max(8, Math.abs(viewportRect[2] - viewportRect[0]))}px`;
      pulse.style.height = `${Math.max(8, Math.abs(viewportRect[3] - viewportRect[1]))}px`;
      textLayer.append(pulse);
      pulse.scrollIntoView({ block: "center", behavior: "smooth" });
      setTimeout(() => pulse.remove(), 1800);
    };
    setTimeout(reveal, 0);
  };

  const getReaderPosition = () => {
    const application = state.application;
    const viewerContainer = document.querySelector("#viewerContainer");
    const page = Number(application?.pdfViewer?.currentPageNumber || 0);
    if (!viewerContainer || !Number.isInteger(page) || page < 1) {
      return "";
    }
    const pageElement = document.querySelector(
      `.page[data-page-number="${page}"]`
    );
    const scrollTop = Number(viewerContainer.scrollTop || 0);
    const pageOffset = pageElement
      ? scrollTop - Number(pageElement.offsetTop || 0)
      : 0;
    const pageOffsetRatio = pageElement?.offsetHeight
      ? pageOffset / Number(pageElement.offsetHeight)
      : 0;
    return JSON.stringify({
      version: 1, page, scrollTop, pageOffset, pageOffsetRatio,
    });
  };

  const restoreReaderPosition = raw => {
    const position = typeof raw === "string" ? parseJson(raw) : raw;
    const page = Number(position?.page);
    const pageOffset = Number(position?.pageOffset || 0);
    const pageOffsetRatio = Number(position?.pageOffsetRatio || 0);
    const fallbackScrollTop = Number(position?.scrollTop || 0);
    if (
      !Number.isInteger(page) || page < 1 ||
      !Number.isFinite(pageOffset) || !Number.isFinite(pageOffsetRatio) ||
      !Number.isFinite(fallbackScrollTop)
    ) {
      return false;
    }
    const application = state.application;
    const viewerContainer = document.querySelector("#viewerContainer");
    if (!application?.pdfViewer || !viewerContainer) return false;
    clearSourceHighlight();
    application.pdfLinkService?.goToPage(page);
    let attempts = 0;
    const restore = () => {
      attempts += 1;
      const pageElement = document.querySelector(
        `.page[data-page-number="${page}"]`
      );
      if (!pageElement && attempts < 30) {
        setTimeout(restore, 50);
        return;
      }
      const desired = pageElement
        ? Number(pageElement.offsetTop || 0) + (
          pageOffsetRatio
            ? Number(pageElement.offsetHeight || 0) * pageOffsetRatio
            : pageOffset
        )
        : fallbackScrollTop;
      const maximum = Math.max(
        0,
        viewerContainer.scrollHeight - viewerContainer.clientHeight
      );
      viewerContainer.scrollTop = Math.max(0, Math.min(maximum, desired));
    };
    setTimeout(restore, 0);
    return true;
  };

  const handleOperationResult = raw => {
    const result = parseJson(raw);
    if (!result) {
      return;
    }
    const requestIdValue = typeof result.requestId === "string"
      ? result.requestId
      : "";
    if (requestIdValue && state.cancelledRequests.has(requestIdValue)) {
      state.cancelledRequests.delete(requestIdValue);
      return;
    }

    const pending = state.pendingAction;
    const matchesPending = Boolean(
      pending &&
      pending.action === result.action &&
      pending.requestId === requestIdValue
    );
    const pendingPayload = matchesPending ? pending.payload : null;
    if (matchesPending) {
      state.pendingAction = null;
      setToolbarPending(false);
    }
    if (!result.ok) {
      showToast(result.error || "Reader action failed.", true);
      return;
    }
    if (result.action === "createHighlight") {
      if (!matchesPending) return;
      showToast("Highlight saved");
      clearSelection();
    } else if (result.action === "createSelectionNote" && result.ok) {
      if (!matchesPending) return;
      if (result.draft) {
        showToast("Write a note to save this selection");
        hideToolbar();
        return;
      }
      if (
        Number.isInteger(result.noteId) &&
        pendingPayload?.location &&
        !state.notes.has(result.noteId)
      ) {
        state.notes.set(result.noteId, {
          id: result.noteId,
          kind: "selection",
          title: "Selection note",
          sourceText: pendingPayload.selectedText || "",
          pageNumber: pendingPayload.location.segments?.[0]?.page || 1,
          location: pendingPayload.location,
        });
        scheduleRepaint();
      }
      showToast("Note created");
      clearTransientSelection(requestIdValue);
      clearSelection();
    } else if (result.action === "translateSelection" && result.ok) {
      if (!matchesPending) return;
      hideToolbar();
    } else if (result.action === "copySelection") {
      if (!matchesPending) return;
      showToast("Copied");
      clearSelection();
    } else if (result.action === "askAISelection") {
      if (!matchesPending) return;
      showToast("Selection attached to AI chat");
      clearSelection();
    }
  };

  const connectBridge = () => {
    if (state.bridge || state.bridgeConnecting) {
      return;
    }
    if (!window.QWebChannel || !window.qt?.webChannelTransport) {
      setTimeout(connectBridge, 50);
      return;
    }
    state.bridgeConnecting = true;
    new window.QWebChannel(window.qt.webChannelTransport, channel => {
      state.bridgeConnecting = false;
      state.bridge = channel.objects.readerBridge;
      if (!state.bridge) {
        showToast("Reader bridge is unavailable.", true);
        return;
      }
      state.bridge.highlightsSnapshot.connect(setHighlights);
      state.bridge.notesSnapshot.connect(setNotes);
      state.bridge.highlightUpserted.connect(upsertHighlight);
      state.bridge.highlightDeleted.connect(removeHighlight);
      state.bridge.navigateRequested.connect(navigateToLocation);
      state.bridge.operationResult.connect(handleOperationResult);
      announceReady();
    });
  };

  const announceReady = () => {
    const application = state.application;
    if (!state.bridge || !application?.pdfDocument) {
      return;
    }
    const fingerprint = application.pdfDocument.fingerprints?.[0] || "";
    if (!fingerprint) {
      return;
    }
    state.documentFingerprint = fingerprint;
    state.bridge.clientReady(
      JSON.stringify({
        pages: application.pdfDocument.numPages,
        fingerprint,
      })
    );
  };

  const connectApplication = () => {
    const application = window.PDFViewerApplication;
    if (!application?.eventBus) {
      setTimeout(connectApplication, 50);
      return;
    }
    state.application = application;
    if (!state.applicationConnected) {
      state.applicationConnected = true;
      application.eventBus.on("documentloaded", () => {
        state.documentFingerprint = "";
        state.highlights.clear();
        state.notes.clear();
        state.transientSelection = null;
        state.pendingAction = null;
        state.sourceHighlight = null;
        document.querySelectorAll(".ra-source-highlight-layer").forEach(layer => layer.remove());
        setToolbarPending(false);
        announceReady();
      });
      application.eventBus.on("pagesinit", announceReady);
      application.eventBus.on("textlayerrendered", event => {
        renderPageAnnotations(event.pageNumber);
        if (state.sourceHighlight?.page === event.pageNumber) {
          renderSourceEvidence(event.pageNumber, state.sourceHighlight.evidence);
        }
      });
      application.eventBus.on("pagechanging", event => {
        callBridge("recordReadingInteraction", "page", false);
        if (state.sourceHighlight && event.pageNumber !== state.sourceHighlight.page) {
          clearSourceHighlight();
        }
      });
      const geometryChanging = () => {
        const restoreToolbar = Boolean(
          state.frozenSelection && state.toolbar && !state.toolbar.hidden
        );
        hideToolbar();
        scheduleRepaint();
        if (state.sourceHighlight) {
          setTimeout(() => renderSourceEvidence(
            state.sourceHighlight.page,
            state.sourceHighlight.evidence
          ), 160);
        }
        if (restoreToolbar) {
          setTimeout(refreshSelectionToolbar, 140);
        }
      };
      application.eventBus.on("scalechanging", geometryChanging);
      application.eventBus.on("rotationchanging", geometryChanging);
      application.eventBus.on("find", () => {
        callBridge("recordReadingInteraction", "search", false);
      });
    }
    announceReady();
  };

  // PDF.js creates a hidden file input and accepts PDF drops even though the
  // Research Assistant skin hides Open File. Replacing the loaded document
  // would otherwise attach annotations to the wrong database paper.
  window.addEventListener("keydown", event => {
    if ((event.ctrlKey || event.metaKey) && !event.shiftKey && event.key.toLowerCase() === "o") {
      event.preventDefault();
      event.stopImmediatePropagation();
      showToast("Open another paper from the Library.");
      return;
    }
    if (event.key === "Escape") {
      clearSourceHighlight();
      hideToolbar();
      if (state.transientSelection) {
        clearTransientSelection(
          state.transientSelection.requestId,
          true,
          true
        );
      }
    }
  }, true);
  document.addEventListener("pointerdown", event => {
    if (!state.sourceHighlight || !event.target?.closest?.("#viewerContainer")) {
      return;
    }
    if (!pointerInsideSourceHighlight(event)) {
      clearSourceHighlight();
    }
  }, true);
  document.addEventListener("dragover", event => {
    if (Array.from(event.dataTransfer?.items || []).some(item => item.type === "application/pdf")) {
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  }, true);
  document.addEventListener("drop", event => {
    if (Array.from(event.dataTransfer?.files || []).some(file => file.type === "application/pdf")) {
      event.preventDefault();
      event.stopImmediatePropagation();
      showToast("Open another paper from the Library.");
    }
  }, true);

  document.addEventListener("selectionchange", () => {
    clearTimeout(document._raSelectionTimer);
    document._raSelectionTimer = setTimeout(refreshSelectionToolbar, 80);
    const selection = window.getSelection();
    if (selection && !selection.isCollapsed && selection.toString().trim()) {
      clearTimeout(document._raReadingSelectionTimer);
      document._raReadingSelectionTimer = setTimeout(
        () => callBridge("recordReadingInteraction", "selection", false), 350
      );
    }
  });
  document.addEventListener("pointerup", event => {
    if (!state.toolbar?.contains(event.target)) {
      setTimeout(refreshSelectionToolbar, 0);
    }
  });
  document.addEventListener("click", event => {
    if (event.defaultPrevented) {
      return;
    }
    const selection = window.getSelection();
    if (selection && !selection.isCollapsed) {
      return;
    }
    const page = event.target?.closest?.(".page[data-page-number]");
    const textLayer = page?.querySelector(".textLayer");
    if (!page || !textLayer) {
      return;
    }
    const layerRect = textLayer.getBoundingClientRect();
    const x = event.clientX - layerRect.left;
    const y = event.clientY - layerRect.top;
    const regions = state.noteHitRegions.get(Number(page.dataset.pageNumber)) || [];
    const hit = regions.find(({ box }) => (
      x >= box.left - 4 && x <= box.right + 4 &&
      y >= box.top - 4 && y <= box.bottom + 4
    ));
    if (hit) {
      callBridge("openNote", hit.noteId, false);
    }
  });
  document.addEventListener("keyup", event => {
    if (event.key.startsWith("Arrow") || event.key === "Shift") {
      setTimeout(refreshSelectionToolbar, 0);
    }
  });
  window.addEventListener("resize", () => {
    if (state.frozenSelection && !state.toolbar?.hidden) {
      positionToolbar(state.frozenSelection);
    }
    scheduleRepaint();
  });
  document.addEventListener("scroll", event => {
    hideToolbar();
    if (event.target?.id === "viewerContainer") {
      clearTimeout(document._raReadingScrollTimer);
      document._raReadingScrollTimer = setTimeout(
        () => callBridge("recordReadingInteraction", "scroll", false), 260
      );
    }
  }, true);

  window.ResearchAssistantReader = {
    clearSelection,
    cancelTransientSelection() {
      const transientRequest = state.transientSelection?.requestId || "";
      if (!transientRequest) {
        return false;
      }
      clearTransientSelection(transientRequest, true, true);
      if (
        state.pendingAction?.action === "translateSelection" &&
        state.pendingAction.requestId === transientRequest
      ) {
        state.pendingAction = null;
        setToolbarPending(false);
      }
      return true;
    },
    openFind() {
      state.application?.findBar?.open();
    },
    zoomIn() {
      state.application?.zoomIn();
    },
    zoomOut() {
      state.application?.zoomOut();
    },
    zoomReset() {
      state.application?.zoomReset();
    },
    zoomAutomatic() {
      if (state.application?.pdfViewer) {
        state.application.pdfViewer.currentScaleValue = "auto";
      }
    },
    fitWidth() {
      if (state.application?.pdfViewer) {
        state.application.pdfViewer.currentScaleValue = "page-width";
      }
    },
    fitPage() {
      if (state.application?.pdfViewer) {
        state.application.pdfViewer.currentScaleValue = "page-fit";
      }
    },
    getReaderPosition,
    restoreReaderPosition,
    repaint: scheduleRepaint,
    showSourceEvidence,
    clearSourceHighlight,
    debugState() {
      return JSON.stringify({
        bridge: Boolean(state.bridge),
        fingerprint: state.documentFingerprint,
        highlights: state.highlights.size,
        notes: state.notes.size,
        toolbarVisible: Boolean(state.toolbar && !state.toolbar.hidden),
        pendingAction: state.pendingAction?.action || "",
        transientSelection: Boolean(state.transientSelection),
        transientKind: state.transientSelection?.kind || "",
      });
    },
  };

  createSelectionToolbar();
  connectBridge();
  connectApplication();
})();
