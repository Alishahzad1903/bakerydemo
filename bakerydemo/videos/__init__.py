"""
Additive capability: turn a published blog article into a short narrated video.

This app layers a video-production feature onto the existing Wagtail Bakery
site without touching the existing pages, blog, images, or admin flows. It
exposes two endpoints on the site's existing v3 HTTP API and produces the
video with VideoGen (stock footage + voice narration).
"""
