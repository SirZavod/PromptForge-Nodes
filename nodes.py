"""
PromptForgeConnection + PromptForgeMultiLoraLoader — ComfyUI custom nodes + HTTP bridge.

Three responsibilities in one package:

1. PASSTHROUGH NODE  (PromptForgeConnection)
   Six inputs (prompt/seed/width/height/negative_prompt/image) → six
   outputs of the same types (image comes with a matching MASK output).
   negative_prompt and image are both optional — not every model/workflow
   uses a negative prompt, and plain text2img workflows have nothing to
   plug into an image input at all. Neither is required to run the node:
   negative_prompt defaults to "" and passes through regardless of
   whether anything is wired to its output; image/mask default to None
   and pass through the same way — whether a picture was actually picked,
   and whether the image/mask outputs are wired to anything downstream,
   are both irrelevant to whether the node runs. If a picture WAS picked,
   it always gets decoded and sent out; this mirrors how negative_prompt
   already behaves.
   PromptForge finds this node by class_type and patches prompt/seed/
   width/height/negative_prompt/image before submitting the workflow to
   /prompt — img2img/img2video mode additionally uploads the chosen file
   to ComfyUI's input/ folder first (via the stock /upload/image route)
   and patches its resulting filename into this same node's `image`
   widget, so one Connector now covers t2i, i2i, and i2v alike.

2. MULTI-LORA LOADER NODE  (PromptForgeMultiLoraLoader)
   Input: model (required), clip (optional), up to N LoRA slots (lora_N_name, lora_N_strength)
   Output: model (required), clip (optional)
   Applies multiple LoRAs sequentially to model and optionally to CLIP.
   CLIP is optional because DiT models don't need it (only UNET+CLIP models do).

3. HTTP BRIDGE  (/promptforge/graph, /promptforge/loras, /promptforge/output_dir)
   The JS extension (web/promptforge_bridge.js) pushes the current canvas
   graph in API format to POST /promptforge/graph every time the graph
   changes. PromptForge retrieves it via GET /promptforge/graph.
   GET /promptforge/loras returns list of available LoRA files.
   GET /promptforge/output_dir returns ComfyUI's real output directory.

4. DYNAMIC LORA SLOTS  (web/promptforge_lora_ui.js)
   PromptForgeMultiLoraLoader exposes LORA_SLOTS (30) optional input pairs
   so the backend can support up to 30 LoRAs, but only 1 pair is shown by
   default. This JS extension hides the rest and reveals them on demand
   via a "+ Add Lora" button, with a delete button on every slot but the
   first. MAX_SLOTS in that file must match LORA_SLOTS below.
"""

import json
import threading
import os
from pathlib import Path

# ── HTTP bridge ───────────────────────────────────────────────────────────────
# Guarded import: PromptServer only exists inside a running ComfyUI process.
# Importing this file outside that context (e.g. unit tests) must not crash.
try:
    from aiohttp import web
    from server import PromptServer
    import folder_paths
    import comfy.sd
    import comfy.utils

    _graph_lock  = threading.Lock()
    _cached_graph = None        # last API-format graph pushed by the browser
    _loras_cache = None         # cached list of available LoRAs
    _loras_cache_lock = threading.Lock()

    routes = PromptServer.instance.routes

    @routes.get("/promptforge/graph")
    async def pf_get_graph(request):
        """PromptForge calls this to retrieve the currently open graph in
        API format, without any manual export from the user."""
        with _graph_lock:
            cached = _cached_graph

        if cached is None:
            # Nothing pushed yet — browser tab may not be open, or the
            # extension hasn't sent its first snapshot.
            return web.Response(
                status=503,
                content_type="application/json",
                text=json.dumps({
                    "error": "no_graph",
                    "detail": (
                        "PromptForge Connection has not received a graph "
                        "snapshot yet. Make sure the ComfyUI browser tab is "
                        "open (the JS extension runs in the browser) and "
                        "reload the tab if needed."
                    ),
                }),
            )

        return web.Response(
            content_type="application/json",
            text=json.dumps({"graph": cached}),
        )

    @routes.post("/promptforge/graph")
    async def pf_push_graph(request):
        """The JS extension POSTs the current graph here after every
        change. We store it and return 200 immediately."""
        global _cached_graph
        try:
            body = await request.json()
            graph = body.get("graph")
            if not isinstance(graph, dict):
                return web.Response(status=400,
                                    text="Expected {\"graph\": {...}}")
            with _graph_lock:
                _cached_graph = graph
            return web.Response(status=200, text="ok")
        except Exception as exc:
            return web.Response(status=400, text=str(exc))

    @routes.get("/promptforge/output_dir")
    async def pf_get_output_dir(request):
        """Returns ComfyUI's real output/ directory (the same one
        folder_paths.get_output_directory() uses to save images), so
        PromptForge's "Open folder" button can point at it directly
        instead of guessing from /system_stats (which doesn't expose
        filesystem paths)."""
        try:
            out_dir = folder_paths.get_output_directory()
            return web.Response(
                content_type="application/json",
                text=json.dumps({"output_dir": out_dir}),
            )
        except Exception as exc:
            return web.Response(
                status=500,
                content_type="application/json",
                text=json.dumps({"error": str(exc)}),
            )

    @routes.get("/promptforge/loras")
    async def pf_get_loras(request):
        """Returns list of available LoRA files from ComfyUI's loras folder.

        Uses folder_paths.get_filename_list("loras") — the exact same source
        as PromptForgeMultiLoraLoader.INPUT_TYPES(), so the names in the app
        dropdown are always bit-for-bit identical to the names the node
        receives at execution time.  No manual filesystem scan needed.

        Response format: {"loras": ["lora1.safetensors", "Anima\\\\x.safetensors", ...]}
        """
        global _loras_cache

        # Use cache if available (avoid repeated filesystem scans)
        with _loras_cache_lock:
            if _loras_cache is not None:
                return web.Response(
                    content_type="application/json",
                    text=json.dumps({"loras": _loras_cache}),
                )

        try:
            # get_filename_list returns names exactly as ComfyUI knows them,
            # which is what the COMBO inputs (and load_lora_for_models) expect.
            loras = list(folder_paths.get_filename_list("loras"))
            loras.sort()

            with _loras_cache_lock:
                _loras_cache = loras

            return web.Response(
                content_type="application/json",
                text=json.dumps({"loras": loras}),
            )
        except Exception as exc:
            return web.Response(
                status=500,
                content_type="application/json",
                text=json.dumps({"error": str(exc)}),
            )

except Exception:
    # Running outside ComfyUI (e.g. unit tests, linters) — skip gracefully.
    pass


# ── passthrough node ──────────────────────────────────────────────────────────

SEED_MIN = 0
SEED_MAX = 2 ** 32 - 1
DIM_MIN  = 64
DIM_MAX  = 8192
DIM_STEP = 8

# LoRA configuration
LORA_SLOTS = 30         # Max LoRA slots. Only 1 is shown by default in the
                         # UI; the rest unlock via the "+ Add Lora" button
                         # (see web/promptforge_lora_ui.js). Keep this in
                         # sync with MAX_SLOTS in that file.
LORA_STRENGTH_MIN = -16.0
LORA_STRENGTH_MAX = 16.0
LORA_STRENGTH_STEP = 0.01
LORA_NONE = "None"      # sentinel combo value meaning "slot empty / skip"

IMAGE_NONE = "None"     # sentinel combo value meaning "no image picked"


class PromptForgeConnection:
    """Pure passthrough: prompt/seed/width/height/negative_prompt/image
    in, the same values out (plus a derived mask for the image).

    PromptForge locates this node by class_type in the graph it fetched
    from /promptforge/graph, patches prompt/seed/width/height/
    negative_prompt/image, then submits the whole graph to ComfyUI's
    /prompt. The node itself just forwards the values (decoding image
    into an actual IMAGE/MASK pair) so they can be wired wherever needed.
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "prompt": ("STRING", {
                    "default": "",
                    "multiline": True,
                    "tooltip": (
                        "Overwritten by PromptForge on submit. "
                        "Type here directly for manual use."
                    ),
                }),
                "seed": ("INT", {
                    "default": 0,
                    "min": SEED_MIN,
                    "max": SEED_MAX,
                    "step": 1,
                    "tooltip": "Overwritten by PromptForge on submit.",
                }),
                "width": ("INT", {
                    "default": 1024,
                    "min": DIM_MIN,
                    "max": DIM_MAX,
                    "step": DIM_STEP,
                }),
                "height": ("INT", {
                    "default": 1024,
                    "min": DIM_MIN,
                    "max": DIM_MAX,
                    "step": DIM_STEP,
                }),
            },
            "optional": {
                # Optional, not required: some models/workflows don't use a
                # negative prompt at all (e.g. several DiT/flow models either
                # ignore it or don't even have a negative conditioning input
                # on their sampler), so this node must not force the user to
                # wire up a negative CLIPTextEncode just to satisfy this
                # input. PromptForge still always sends a value (usually an
                # empty string) when patching the graph, but a manual/
                # ComfyUI-side user can simply leave it unconnected.
                "negative_prompt": ("STRING", {
                    "default": "",
                    "multiline": True,
                    "tooltip": (
                        "Overwritten by PromptForge on submit. "
                        "Wire to a negative CLIPTextEncode. Optional — "
                        "leave unconnected for workflows/models that don't "
                        "use a negative prompt."
                    ),
                }),
                # Optional the same way negative_prompt is: plain text2img
                # workflows have nothing to plug an image into at all, so
                # this must never be forced. Whether a file is actually
                # selected here, and whether the image/mask OUTPUTS below
                # are wired to anything downstream, are both irrelevant to
                # whether the node runs — same "always passes through,
                # nobody's forced to use it" contract as negative_prompt.
                # Listed same as stock LoadImage's own combo (files under
                # ComfyUI's input/ directory), with "None" prepended as the
                # sentinel for "no image picked" — PromptForge's own i2i/i2v
                # mode uploads a file here first (via /upload/image) and
                # patches this widget with the resulting filename; a plain
                # t2i submission just leaves it at "None".
                "image": (cls._image_choices(), {
                    "default": IMAGE_NONE,
                    "image_upload": True,
                    "tooltip": (
                        "Overwritten by PromptForge on submit for img2img/"
                        "img2video. Optional — leave as 'None' for plain "
                        "text2image."
                    ),
                }),
            },
        }

    @classmethod
    def _image_choices(cls):
        """List of files under ComfyUI's input/ directory for the `image`
        combo, same source stock LoadImage uses (folder_paths), with
        IMAGE_NONE prepended as the sentinel for "no image picked" — this
        node has to work for plain text2img graphs too, where there's
        simply nothing to select."""
        try:
            input_dir = folder_paths.get_input_directory()
            files = [
                f for f in os.listdir(input_dir)
                if os.path.isfile(os.path.join(input_dir, f))
            ]
            files.sort()
        except Exception as e:
            print(f"[PromptForgeConnection] Error listing input images: {e}")
            files = []
        return [IMAGE_NONE] + files

    RETURN_TYPES  = ("STRING", "INT",  "INT",   "INT",   "STRING",         "IMAGE", "MASK")
    RETURN_NAMES  = ("prompt", "seed", "width", "height", "negative_prompt", "image", "mask")
    FUNCTION      = "passthrough"
    CATEGORY      = "PromptForge"
    DESCRIPTION   = (
        "Entry point for PromptForge generations. Wire the outputs into "
        "CLIPTextEncode / KSampler / EmptyLatentImage as needed. "
        "The negative_prompt output connects to a negative CLIPTextEncode "
        "and is optional — leave it unconnected for models/workflows that "
        "don't use a negative prompt. The image/mask outputs are for img2img "
        "/img2video — also optional, leave unconnected for plain text2image. "
        "Works as a normal node for manual use too."
    )

    @staticmethod
    def _load_image(image_name):
        """Mirrors stock LoadImage.load_image closely enough to behave
        identically for anyone who's used that node before: PIL-load →
        exif_transpose → RGB tensor, plus an alpha-channel MASK (or an
        all-zero stub mask when the source has no alpha). Returns
        (None, None) for the IMAGE_NONE sentinel/empty name — a plain
        text2img submission never needed a file here at all."""
        if not image_name or image_name == IMAGE_NONE:
            return None, None

        from PIL import Image, ImageOps, ImageSequence
        import numpy as np
        import torch

        image_path = folder_paths.get_annotated_filepath(image_name)
        img = Image.open(image_path)

        output_images = []
        output_masks = []
        w, h = None, None
        for i in ImageSequence.Iterator(img):
            i = ImageOps.exif_transpose(i)
            if i.mode == 'I':
                i = i.point(lambda px: px * (1 / 255))
            frame = i.convert("RGB")

            if len(output_images) == 0:
                w, h = frame.size
            if frame.size[0] != w or frame.size[1] != h:
                continue

            arr = np.array(frame).astype(np.float32) / 255.0
            tensor = torch.from_numpy(arr)[None,]
            if 'A' in i.getbands():
                mask = np.array(i.getchannel('A')).astype(np.float32) / 255.0
                mask = 1. - torch.from_numpy(mask)
            else:
                mask = torch.zeros((64, 64), dtype=torch.float32, device="cpu")
            output_images.append(tensor)
            output_masks.append(mask.unsqueeze(0))

        if len(output_images) > 1 and img.format not in ('MPO',):
            return torch.cat(output_images, dim=0), torch.cat(output_masks, dim=0)
        return output_images[0], output_masks[0]

    def passthrough(self, prompt, seed, width, height, negative_prompt="", image=IMAGE_NONE):
        img_tensor, mask_tensor = self._load_image(image)
        return (prompt, seed, width, height, negative_prompt, img_tensor, mask_tensor)


class PromptForgeMultiLoraLoader:
    """Load multiple LoRAs and apply them sequentially to a model.

    Each lora_N_name is a searchable COMBO populated from ComfyUI's own
    indexed LoRA list (folder_paths.get_filename_list("loras")) — the same
    source and the same type-to-filter dropdown the built-in LoraLoader node
    uses. Selecting "None" (the default) leaves that slot empty.

    CLIP input/output is OPTIONAL because:
    - UNET models: CLIP weights are trained during LoRA fine-tune → must patch CLIP
    - DiT models: Only the base model layers are trained → CLIP input is ignored
    
    This node accepts CLIP if provided but doesn't require it. If CLIP is not
    connected, patching is skipped gracefully.
    """

    @classmethod
    def _lora_choices(cls):
        """List of available LoRA files for the combo dropdowns, using the
        same indexed/searchable source as ComfyUI's own LoraLoader node
        (folder_paths.get_filename_list), so the widget gets the standard
        type-to-filter dropdown for free — no extra JS required.
        "None" is prepended as the sentinel meaning "slot empty / skip"."""
        try:
            choices = folder_paths.get_filename_list("loras")
        except Exception as e:
            print(f"[PromptForgeMultiLoraLoader] Error listing LoRAs: {e}")
            choices = []
        return [LORA_NONE] + list(choices)

    @classmethod
    def INPUT_TYPES(cls):
        """
        Required inputs: model
        Optional inputs: clip
        Repeated slots: lora_N_name (combo, indexed list) + lora_N_strength (float)
        """
        lora_choices = cls._lora_choices()

        inputs = {
            "required": {
                "model": ("MODEL",),
            },
            "optional": {
                "clip": ("CLIP",),  # Optional: only needed for UNET models
            }
        }
        
        # Add N LoRA slots
        for i in range(1, LORA_SLOTS + 1):
            inputs["optional"][f"lora_{i}_name"] = (lora_choices, {
                "default": LORA_NONE,
                "tooltip": f"LoRA file #{i} to apply ('{LORA_NONE}' to skip).",
            })
            inputs["optional"][f"lora_{i}_strength"] = ("FLOAT", {
                "default": 1.0,
                "min": LORA_STRENGTH_MIN,
                "max": LORA_STRENGTH_MAX,
                "step": LORA_STRENGTH_STEP,
                "tooltip": f"Strength of LoRA #{i}",
            })
        
        return inputs

    RETURN_TYPES = ("MODEL", "CLIP")
    RETURN_NAMES = ("model", "clip")
    FUNCTION = "load_loras"
    CATEGORY = "PromptForge"
    DESCRIPTION = (
        "Load and apply multiple LoRAs sequentially to a base model. "
        "CLIP input/output is optional (needed only for UNET models, "
        "not for DiT). Pick 'None' in a slot's dropdown to skip it. "
        "Strength range: "
        f"{LORA_STRENGTH_MIN:+.0f} to {LORA_STRENGTH_MAX:+.0f}."
    )

    def __init__(self):
        # path -> loaded state dict, so re-running with the same LoRAs
        # doesn't re-read the file from disk every single execution.
        self._lora_cache = {}

    def load_loras(self, model, clip=None, **kwargs):
        """
        Apply LoRAs in sequence.
        
        Args:
            model: Base model to patch
            clip: CLIP model (optional, may be None)
            **kwargs: lora_N_name and lora_N_strength for each slot
        
        Returns:
            (patched_model, patched_clip or original clip)
        """
        # Collect non-empty LoRA slots
        loras_to_apply = []
        for i in range(1, LORA_SLOTS + 1):
            lora_name = kwargs.get(f"lora_{i}_name", LORA_NONE)
            lora_strength = kwargs.get(f"lora_{i}_strength", 1.0)
            
            if lora_name and lora_name != LORA_NONE:  # Skip empty/"None" slots
                loras_to_apply.append((lora_name, lora_strength))
        
        # Apply each LoRA
        current_model = model
        current_clip = clip
        
        for lora_name, strength in loras_to_apply:
            try:
                # Resolve the file path, then actually READ the weights off
                # disk. load_lora_for_models needs the loaded state dict
                # (a dict of tensors), not the path string — passing the
                # path directly (the previous bug) fails inside comfy.sd
                # and gets swallowed by the except below, so the LoRA
                # silently never applies and the output never changes.
                lora_path = folder_paths.get_full_path("loras", lora_name)
                
                if lora_path is None:
                    print(f"[PromptForgeMultiLoraLoader] Warning: LoRA not found: {lora_name}")
                    continue

                lora_sd = self._lora_cache.get(lora_path)
                if lora_sd is None:
                    lora_sd = comfy.utils.load_torch_file(lora_path, safe_load=True)
                    self._lora_cache[lora_path] = lora_sd
                
                # Apply to model
                current_model, current_clip_patched = comfy.sd.load_lora_for_models(
                    current_model,
                    current_clip,
                    lora_sd,
                    strength,
                    strength  # Apply same strength to both UNET and CLIP
                )
                
                # Update CLIP only if it was provided and returned by load_lora_for_models
                if current_clip_patched is not None:
                    current_clip = current_clip_patched
                
            except Exception as e:
                print(f"[PromptForgeMultiLoraLoader] Error applying LoRA '{lora_name}': {e}")
                continue
        
        return (current_model, current_clip)


# ── registrations ─────────────────────────────────────────────────────────────

NODE_CLASS_MAPPINGS = {
    "PromptForgeConnection": PromptForgeConnection,
    "PromptForgeMultiLoraLoader": PromptForgeMultiLoraLoader,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "PromptForgeConnection": "PromptForge Connector",
    "PromptForgeMultiLoraLoader": "PromptForge Multi Lora Loader",
}
