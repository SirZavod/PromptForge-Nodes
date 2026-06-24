"""
PromptForge — ComfyUI custom node package.

Install by copying this whole folder into ComfyUI/custom_nodes/, e.g.:

    ComfyUI/custom_nodes/PromptForge/
    ├── __init__.py
    ├── nodes.py
    ├── web/
    │   └── promptforge_bridge.js
    ├── pyproject.toml
    └── README.md

Then restart ComfyUI and reload the browser tab.

The nodes appear under Add Node → PromptForge → PromptForge Connector
and Add Node → PromptForge → PromptForge Multi Lora Loader.
The HTTP bridge (/promptforge/graph) is registered automatically.
"""

from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

# WEB_DIRECTORY tells ComfyUI to serve everything in web/ as a static JS
# extension that is automatically loaded by the browser frontend.
WEB_DIRECTORY = "web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
