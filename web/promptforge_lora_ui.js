/**
 * PromptForge LoRA Slots UI — ComfyUI frontend extension.
 *
 * PromptForgeMultiLoraLoader (Python side) exposes a fixed MAX_SLOTS pairs
 * of optional inputs (lora_N_name / lora_N_strength) so it can support up
 * to MAX_SLOTS LoRAs — but a node with 30 pairs of widgets visible at once
 * is unusable. This extension hides every slot beyond the first on node
 * creation, and gives the node:
 *
 *   - a "+ Add Lora" button that reveals the next slot (up to MAX_SLOTS)
 *   - a 🗑 delete button on every slot except the first, which clears that
 *     slot, shifts every slot above it down by one, and hides the last row
 *
 * This is the same "hide widget via type + computeSize hijack" technique
 * used by other multi-slot ComfyUI nodes (e.g. rgthree's Power Lora
 * Loader) — there's no official ComfyUI API for hiding widgets yet, so
 * this leans on the de-facto community convention instead.
 *
 * IMPORTANT: MAX_SLOTS below must match LORA_SLOTS in nodes.py.
 */

import { app } from "../../scripts/app.js";

const NODE_NAME  = "PromptForgeMultiLoraLoader";
const MAX_SLOTS  = 30;          // keep in sync with LORA_SLOTS in nodes.py
const NONE_VALUE = "None";      // keep in sync with LORA_NONE in nodes.py
const HIDDEN_TAG = "promptforge-hidden";

// ── widget hide/show ────────────────────────────────────────────────────────

function hideWidget(widget) {
    if (!widget || (widget.type && widget.type.startsWith(HIDDEN_TAG))) return;
    widget.pf_origType = widget.type;
    widget.pf_origComputeSize = widget.computeSize;
    widget.type = HIDDEN_TAG;
    widget.computeSize = () => [0, -4];
}

function showWidget(widget) {
    if (!widget || !widget.type || !widget.type.startsWith(HIDDEN_TAG)) return;
    widget.type = widget.pf_origType;
    widget.computeSize = widget.pf_origComputeSize;
}

function isHidden(widget) {
    return !!(widget && widget.type && widget.type.startsWith(HIDDEN_TAG));
}

function resize(node) {
    const size = node.computeSize();
    node.setSize([Math.max(node.size[0], size[0]), size[1]]);
    node.setDirtyCanvas(true, true);
}

// ── slot lookup ──────────────────────────────────────────────────────────────

function slotWidgets(node, i) {
    return {
        name: node.widgets.find((w) => w.name === `lora_${i}_name`),
        strength: node.widgets.find((w) => w.name === `lora_${i}_strength`),
    };
}

// ── core logic ───────────────────────────────────────────────────────────────

function addSlot(node) {
    const active = node.pfActiveSlots ?? 1;
    if (active >= MAX_SLOTS) return;

    const next = active + 1;
    const { name, strength } = slotWidgets(node, next);
    showWidget(name);
    showWidget(strength);
    showWidget(node.pfDeleteButtons?.[next]);

    node.pfActiveSlots = next;
    if (next >= MAX_SLOTS) hideWidget(node.pfAddButton);

    resize(node);
}

function removeSlot(node, i) {
    const active = node.pfActiveSlots ?? 1;
    if (active <= 1 || i > active) return; // always keep slot 1

    // Shift every slot above i down by one (so the active range stays
    // 1..active-1 contiguous), then clear + hide whichever slot is now
    // the spare one at the end.
    for (let j = i; j < active; j++) {
        const cur = slotWidgets(node, j);
        const nxt = slotWidgets(node, j + 1);
        if (cur.name && nxt.name) cur.name.value = nxt.name.value;
        if (cur.strength && nxt.strength) cur.strength.value = nxt.strength.value;
    }

    const last = slotWidgets(node, active);
    if (last.name) last.name.value = NONE_VALUE;
    if (last.strength) last.strength.value = 1.0;
    hideWidget(last.name);
    hideWidget(last.strength);
    hideWidget(node.pfDeleteButtons?.[active]);

    node.pfActiveSlots = active - 1;
    showWidget(node.pfAddButton); // we just freed a slot, so Add is always valid again

    resize(node);
}

/** Re-derive which slots should be visible from the widgets' actual values
 *  (used right after a saved workflow is loaded, since slot visibility
 *  is UI-only state that isn't part of the saved widget values). */
function syncFromValues(node) {
    let highestUsed = 1;
    for (let i = MAX_SLOTS; i >= 1; i--) {
        const { name } = slotWidgets(node, i);
        if (name && name.value && name.value !== NONE_VALUE) {
            highestUsed = i;
            break;
        }
    }
    node.pfActiveSlots = highestUsed;

    for (let i = 2; i <= MAX_SLOTS; i++) {
        const { name, strength } = slotWidgets(node, i);
        const del = node.pfDeleteButtons?.[i];
        if (i <= highestUsed) {
            showWidget(name);
            showWidget(strength);
            showWidget(del);
        } else {
            hideWidget(name);
            hideWidget(strength);
            hideWidget(del);
        }
    }

    if (node.pfAddButton) {
        if (highestUsed >= MAX_SLOTS) hideWidget(node.pfAddButton);
        else showWidget(node.pfAddButton);
    }

    resize(node);
}

/** One-time setup when the node is first created: hide slots 2..MAX_SLOTS,
 *  create one delete button per slot (2..MAX_SLOTS) positioned right after
 *  that slot's strength widget, and the "+ Add Lora" button at the end. */
function setupNode(node) {
    if (node.pfLoraSetupDone) return;
    node.pfLoraSetupDone = true;
    node.pfActiveSlots = 1;
    node.pfDeleteButtons = {};

    for (let i = 2; i <= MAX_SLOTS; i++) {
        const { name, strength } = slotWidgets(node, i);
        hideWidget(name);
        hideWidget(strength);

        if (!strength) continue;
        const delBtn = node.addWidget("button", `\uD83D\uDDD1 Remove LoRA #${i}`, null, () => removeSlot(node, i));
        delBtn.pf_slot = i;
        // addWidget() appends at the end of node.widgets — move it right
        // after this slot's strength widget so it renders in the right row.
        const insertAt = node.widgets.indexOf(strength) + 1;
        node.widgets.splice(node.widgets.indexOf(delBtn), 1);
        node.widgets.splice(insertAt, 0, delBtn);
        node.pfDeleteButtons[i] = delBtn;
        hideWidget(delBtn);
    }

    node.pfAddButton = node.addWidget("button", "+ Add Lora", null, () => addSlot(node));

    resize(node);
}

// ── extension registration ────────────────────────────────────────────────────

app.registerExtension({
    name: "promptforge.loraSlots",

    beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== NODE_NAME) return;

        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = onNodeCreated ? onNodeCreated.apply(this, arguments) : undefined;
            setupNode(this);
            return r;
        };

        // Saved workflows restore widget values AFTER onNodeCreated, via
        // configure() → onConfigure(). Re-derive visible slot count then.
        const onConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function (info) {
            const r = onConfigure ? onConfigure.apply(this, arguments) : undefined;
            syncFromValues(this);
            return r;
        };
    },
});
