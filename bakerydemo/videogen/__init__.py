"""VideoGen integration for turning published blog articles into short videos.

This is an additive capability: it exposes a small asynchronous video-production
flow on the existing Wagtail v3 HTTP API and talks to the VideoGen provider. It
does not alter any existing page, blog, image or admin behaviour.
"""
