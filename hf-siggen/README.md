# HF Email Signature Generator

A single-page tool for Highlands Fellowship staff to generate a branded email signature and copy it into their preferred email client.

**Live:** https://highlands-fellowship.github.io/highlands-misc/hf-siggen/

## Usage

1. Open the page in a browser.
2. Fill in your name, title, campus/location (optional), and contact details.
3. Click **Copy Signature (Recommended)** and paste into your email client.

Installation instructions for Outlook (Windows/Mac), Microsoft 365, Apple Mail (macOS), and Apple Mail (iOS) are available on the page itself.

## Details

- Single `index.html` — no build step, no dependencies, no backend.
- Office phone (`276-628-3297`) is fixed for all staff; individuals add an optional extension or alternate phone.
- Branding follows the [Highlands Fellowship Brand Kit](https://github.com/Highlands-Fellowship/highlands-brand).
- Logo served from Cloudinary via the brand kit's canonical URL.

## Deployment

Enable GitHub Pages on this repo (Settings → Pages → Deploy from branch `main`, folder `/`). The tool will be available at the URL above.

## Inspired by

[VRL-SigGen](https://github.com/valleyreallife/VRL-SigGen) by Valley Real Life.
