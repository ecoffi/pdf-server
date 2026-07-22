# PDF Library Server

A lightweight, self-hosted, tablet-friendly web application for reading and managing a PDF library. The server automatically scans a directory for PDFs, serves pages as optimized JPEG images to save bandwidth, tracks reading progress per client using cookies, and supports fluent touchscreen gestures or keyboard browsing.

## Features

- **Automatic Scanning:** Recursively monitors a library directory to index files, handles updated files, and invalidates old image caches atomically.
- **Optimized Rendering:** Converts PDF pages to crisp JPEG formats on demand using `PyMuPDF` to prevent heavy PDF downloads on mobile devices.
- **Smart Progress Tracking:** Remembers your exact position across multiple documents without requiring user accounts or logins.
- **"Continue Reading" Section:** Highlights your top 5 most recently read documents right at the top of the homepage for seamless return access.
- **Immersive Viewports:** Touch-friendly page controls, arrow key hotkeys, and a responsive SVG-driven Fullscreen mode toggle.

## Setup & Installation

1. **Clone or copy the source files** into your desired deployment directory.
2. **Install the required dependencies** using `pip`:

   ```bash
   pip install Flask PyMuPDF
   ```

3. Configure your storage targets using environment variables (optional, defaults to relative paths):
    PDF_LIBRARY_DIR: Directory containing your structured sub-folders and PDF documents (Default: ./library).
    PDF_DATA_DIR: Directory where reading logs, caches, and structural indices will be written (Default: ./data).
