// Cloudflare Worker для сетей — вставьте ВЕСЬ файл в Edit code и нажмите Deploy.
// GitHub выполняет фильтрацию. Worker выдает готовый XML без тяжелой обработки.
const MANIFEST_URL = "https://raw.githubusercontent.com/Loungechill/WilliamsOliver/main/segmented_feeds.json";
const CHANNEL = "networks";
const ASSET_NAME = "feed_networks.xml";

export default {
  async fetch(request) {
    const path = new URL(request.url).pathname;
    if (path !== "/" && path !== "/feed.xml") return new Response("Use /feed.xml", { status: 404 });
    if (!["GET", "HEAD"].includes(request.method)) {
      return new Response("Method not allowed", { status: 405, headers: { Allow: "GET, HEAD" } });
    }
    try {
      const metadata = await fetch(MANIFEST_URL, { cf: { cacheTtl: 0 }, signal: AbortSignal.timeout(30000) });
      if (!metadata.ok) throw new Error("Сначала запустите GitHub Actions: Update segmented feeds");
      const manifest = await metadata.json();
      const url = manifest[CHANNEL]?.url;
      const allowed = /^https:\/\/github\.com\/Loungechill\/WilliamsOliver\/releases\/download\/[^/]+\//;
      if (typeof url !== "string" || !allowed.test(url) || !url.endsWith("/" + ASSET_NAME)) {
        throw new Error("Некорректная ссылка на готовый фид");
      }
      const feed = await fetch(url, {
        method: request.method, redirect: "follow", signal: AbortSignal.timeout(180000),
      });
      if (!feed.ok) throw new Error(`GitHub XML: HTTP ${feed.status}`);
      if (/text\/html/i.test(feed.headers.get("content-type") || "")) {
        if (feed.body) await feed.body.cancel();
        throw new Error("GitHub вернул HTML вместо XML");
      }
      return new Response(feed.body, {
        headers: {
          "Content-Type": "application/xml; charset=utf-8",
          "Cache-Control": "no-store",
          "X-Content-Type-Options": "nosniff",
          "X-WO-Channel": CHANNEL,
          "X-WO-Updated-At": String(manifest.generated_at || ""),
          "X-WO-Offers": String(manifest[CHANNEL].remaining_offers),
          "X-WO-Impressions-Threshold": String(manifest[CHANNEL].threshold),
        },
      });
    } catch (error) {
      return new Response("Не удалось получить фид: " + error.message, {
        status: 502, headers: { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store" },
      });
    }
  },
};
