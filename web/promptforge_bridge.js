/**
 * Prompt Forge Bridge — ComfyUI frontend extension.
 *
 * Architecture:
 *   PUSH model — every time the graph changes, this extension serializes
 *   it into API format (via app.graphToPrompt()) and POSTs the result to
 *   the Python route POST /promptforge/graph on the ComfyUI server.
 *   The Python side stores the last received graph in memory.
 *   Prompt Forge then retrieves it with a plain GET /promptforge/graph
 *   at generation time — no manual "Export (API)" step, no saved file.
 *
 * Why PUSH instead of PULL:
 *   The Python HTTP handler runs in a different process context from the
 *   browser JS. It cannot read window.* directly. The browser can talk
 *   to the Python server over HTTP, so the browser pushes; Python caches;
 *   the desktop app pulls. Clean separation.
 */

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const EXT_NAME  = "promptforge.bridge";
const PUSH_PATH = "/promptforge/graph";   // matches the Python route

// ── graph push ───────────────────────────────────────────────────────────────

let _pushPending = false;

async function pushGraph() {
    if (_pushPending) return;   // don't pile up concurrent pushes
    _pushPending = true;
    try {
        const result = await app.graphToPrompt();
        const apiGraph = result?.output;
        if (!apiGraph) return;

        await fetch(PUSH_PATH, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            // Send the raw API-format graph object.
            body: JSON.stringify({ graph: apiGraph }),
        });
    } catch (e) {
        // Silently ignore — ComfyUI may not be fully initialised yet,
        // or the route may not exist if the extension isn't installed.
        if (e?.name !== "TypeError") {   // TypeError = fetch itself failed (offline)
            console.warn("[PromptForge] pushGraph error:", e);
        }
    } finally {
        _pushPending = false;
    }
}

/** Debounce: coalesce rapid widget edits into a single push. */
function debounce(fn, ms) {
    let t = null;
    return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}
const debouncedPush = debounce(pushGraph, 300);

// ── extension registration ────────────────────────────────────────────────────

app.registerExtension({
    name: EXT_NAME,

    async setup() {
        // Initial push once the app is ready.
        await pushGraph();

        // Hook graph-level events (node added / removed / linked).
        const graph = app.graph;
        if (graph) {
            const _onAdd    = graph.onNodeAdded?.bind(graph);
            const _onRemove = graph.onNodeRemoved?.bind(graph);
            graph.onNodeAdded    = (n, ...a) => { _onAdd?.(n, ...a);    debouncedPush(); };
            graph.onNodeRemoved  = (n, ...a) => { _onRemove?.(n, ...a); debouncedPush(); };
        }

        // Canvas afterChange covers link draws/removals and node moves.
        const canvas = app.canvas;
        if (canvas) {
            const _after = canvas.onAfterChange?.bind(canvas);
            canvas.onAfterChange = (...a) => { _after?.(...a); debouncedPush(); };
        }

        // ComfyUI API events.
        api.addEventListener("promptQueued",   () => pushGraph());       // always fresh before queue
        api.addEventListener("graphCleared",   debouncedPush);
        api.addEventListener("workflowLoaded", debouncedPush);

        console.log("[PromptForge] Bridge loaded — live graph will be " +
                    "pushed to " + PUSH_PATH + " on every change.");
    },

    // Per-node widget change hook.
    nodeCreated(node) {
        const _orig = node.onWidgetChanged?.bind(node);
        node.onWidgetChanged = (...a) => { _orig?.(...a); debouncedPush(); };
    },
});
