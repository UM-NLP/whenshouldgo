// Foreground location, read only inside a user tap handler. Never a continuous watch, never stored,
// never logged: the pair exists in the memory of the request that carries it.

export interface Coordinates {
  latitude: number;
  longitude: number;
}

export type GeoErrorKind = "unsupported" | "denied" | "unavailable" | "timeout";

export class GeoError extends Error {
  constructor(readonly kind: GeoErrorKind) {
    super(`geolocation ${kind}`);
    this.name = "GeoError";
  }
}

export function getForegroundPosition(): Promise<Coordinates> {
  return new Promise((resolve, reject) => {
    const geo = typeof navigator === "undefined" ? undefined : navigator.geolocation;
    if (!geo) {
      reject(new GeoError("unsupported"));
      return;
    }
    geo.getCurrentPosition(
      (position) =>
        resolve({ latitude: position.coords.latitude, longitude: position.coords.longitude }),
      (error) =>
        reject(
          new GeoError(error.code === 1 ? "denied" : error.code === 3 ? "timeout" : "unavailable"),
        ),
      { enableHighAccuracy: false, timeout: 10_000, maximumAge: 60_000 },
    );
  });
}

export function geoErrorText(kind: GeoErrorKind): string {
  switch (kind) {
    case "denied":
      return "Your browser blocked location access. You can allow it in the site settings, or keep using the app by naming an area in your message.";
    case "timeout":
      return "Finding your location took too long. Try again, or name an area in your message.";
    case "unsupported":
      return "This browser can't share a location. Name an area in your message instead.";
    default:
      return "Your location isn't available right now. Try again, or name an area in your message.";
  }
}
