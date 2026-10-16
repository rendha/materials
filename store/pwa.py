"""Makes the website installable (a PWA): the manifest, the service worker and the offline page.
The service worker only keeps the icons and the offline page.  The pages with stock / rates / customers are NEVER stored,
so nobody sees old numbers or another user's data."""
import json

from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.templatetags.static import static

CACHE_VERSION = "v1"          # change this text when the icons / offline page change, so phones fetch the new ones


def manifest(request):
    data = {
        "name": "Dehlsen Materials",
        "short_name": "Materials",
        "description": "Materials, warehouse and purchase lists for Dehlsen Energy projects",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "any",
        "background_color": "#3B73B9",
        "theme_color": "#3B73B9",
        "icons": [
            {"src": static("store/icon-192.png"), "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": static("store/icon-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    }
    return JsonResponse(data, content_type="application/manifest+json")


def service_worker(request):
    files = [static("store/icon-192.png"), static("store/icon-512.png"), "/offline/"]
    script = """
const CACHE = "dehlsen-materials-%(version)s";
const FILES = %(files)s;

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(FILES)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((names) => Promise.all(names.filter((n) => n !== CACHE).map((n) => caches.delete(n)))).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") { return; }                       // saving a sheet always goes to the server
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) { return; }
  if (request.mode === "navigate") {                              // pages: always from the server, the offline page only when there is no network
    event.respondWith(fetch(request).catch(() => caches.match("/offline/")));
    return;
  }
  if (url.pathname.startsWith("/static/")) {                      // icons / admin files: the stored copy first
    event.respondWith(caches.match(request).then((hit) => hit || fetch(request)));
  }
});
""" % {"version": CACHE_VERSION, "files": json.dumps(files)}
    response = HttpResponse(script, content_type="application/javascript")
    response["Service-Worker-Allowed"] = "/"
    response["Cache-Control"] = "no-cache"
    return response


def offline(request):
    return render(request, "store/offline.html")
