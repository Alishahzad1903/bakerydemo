"""Additive capability: turn a published blog article into a short narrated MP4.

This app exposes two endpoints on the existing Wagtail v3 HTTP API for producing
and retrieving a VideoGen-generated video of a single blog article. It does not
touch the existing pages, blog, images or admin flows.
"""

default_app_config = "bakerydemo.videos.apps.VideosConfig"
