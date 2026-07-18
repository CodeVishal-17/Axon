"""Source adapters. ``base`` (T1.2) defines the normalized types and the
SourceAdapter protocol; provider packages (github, later notion/slack/...)
implement it. Nothing below this layer may see provider-specific types."""
