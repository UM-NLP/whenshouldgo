import type { PlaceCard, RouteCheck } from "../../board/boardApi";
import { boardFailureText, canRetry, freshness, routeSummary, showsBetaWarning, WALK_BETA_TEXT } from "../../board/viewModels";
import type { CandidateCheck } from "../../useBoard";
import type { LocationConsent } from "../../useLocationConsent";

/**
 * Server-rendered place facts as TEXT (never HTML). The "Google Maps" attribution comes immediately
 * after the fields, with nothing between them, then any attribution strings the server supplied.
 */
export function PlaceCardView({ card, now }: { card: PlaceCard; now: number }) {
  const fresh = freshness(card.fetched_at, now);
  return (
    <div className="place-card">
      <div className="place-fields">
        <p className="place-name">{card.display_name}</p>
        {card.formatted_address ? <p className="place-line">{card.formatted_address}</p> : null}
        {card.open_now === null || card.open_now === undefined ? null : (
          <p className="place-line">{card.open_now ? "Open now" : "Closed now"}</p>
        )}
        {card.rating === null || card.rating === undefined ? null : <p className="place-line">Rated {card.rating} out of 5</p>}
      </div>
      <p className="place-attribution">
        <span translate="no">Google Maps</span>
      </p>
      {card.attributions.map((text, i) => (
        <p key={`${i}-${text}`} className="place-attribution-extra">
          {text}
        </p>
      ))}
      <p className={fresh.stale ? "fresh stale" : "fresh"}>{fresh.label}</p>
    </div>
  );
}

export function RouteCardView({ route, now }: { route: RouteCheck; now: number }) {
  const fresh = freshness(route.fetched_at, now);
  return (
    <div className="route-card">
      <p className="route-summary">{routeSummary(route)}</p>
      <p className="place-attribution">
        <span translate="no">Google Maps</span>
      </p>
      {route.attributions.map((text, i) => (
        <p key={`${i}-${text}`} className="place-attribution-extra">
          {text}
        </p>
      ))}
      {showsBetaWarning(route) ? (
        <p className="notice notice-warning" role="status">
          {WALK_BETA_TEXT}
        </p>
      ) : null}
      <p className={fresh.stale ? "fresh stale" : "fresh"}>{fresh.label}</p>
    </div>
  );
}

interface CheckProps {
  check: CandidateCheck | undefined;
  location: LocationConsent;
  now: number;
  onRefresh(): void;
  onCheckRoute(): void;
}

/** One candidate's details and drive check, with every state spelled out in fixed copy. */
export function CheckView({ check, location, now, onRefresh, onCheckRoute }: CheckProps) {
  if (!check) return null;
  const { card, route } = check;
  return (
    <div className="check">
      {card.status === "loading" ? (
        <p className="loading" role="status">
          Loading place details...
        </p>
      ) : card.status === "ok" ? (
        <PlaceCardView card={card.data} now={now} />
      ) : card.status === "failed" ? (
        <div className="notice notice-error" role="alert">
          <p>{boardFailureText(card.failure, "place")}</p>
          {card.failure.kind === "unavailable" || !canRetry(card.failure) ? null : (
            <button type="button" onClick={onRefresh}>
              Try again
            </button>
          )}
        </div>
      ) : null}

      {route.status === "loading" ? (
        <p className="loading" role="status">
          Checking the drive...
        </p>
      ) : route.status === "ok" ? (
        <RouteCardView route={route.data} now={now} />
      ) : route.status === "needs_consent" ? (
        <div className="notice" role="status">
          {location.phase === "on" ? (
            <>
              <p>Check how long the drive would take from where you are.</p>
              <button type="button" onClick={onCheckRoute}>
                Check the drive
              </button>
            </>
          ) : (
            <>
              <p>To see the drive time, turn on your location. It is used for this check only and is not stored.</p>
              <button type="button" onClick={location.start} disabled={location.phase === "unknown" || location.phase === "working"}>
                Use my location
              </button>
            </>
          )}
        </div>
      ) : route.status === "geo" ? (
        <div className="notice notice-error" role="alert">
          <p>{route.text}</p>
          <button type="button" onClick={onCheckRoute}>
            Try again
          </button>
        </div>
      ) : route.status === "failed" ? (
        <div className="notice notice-error" role="alert">
          <p>{boardFailureText(route.failure, "route")}</p>
          {route.failure.kind === "unavailable" || !canRetry(route.failure) ? null : (
            <button type="button" onClick={onCheckRoute}>
              Try again
            </button>
          )}
        </div>
      ) : null}
      {card.status === "ok" && route.status === "failed" ? (
        <p className="muted">Some details could not be loaded. The place information above is still current.</p>
      ) : null}
    </div>
  );
}
