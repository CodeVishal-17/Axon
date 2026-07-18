"""LLM layer. ``provider`` (T1.1) exposes complete()/embed() and is the ONLY
module in the codebase allowed to import an LLM SDK. Prompts live in
``prompts/`` as versioned files — they are core IP and get code review."""
