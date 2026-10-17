import type { LocationConsent } from "../useLocationConsent";

// PLACEHOLDER notice text for notice version "location-notice-1". Counsel has not reviewed it
// (privacy notice, issue #27). It must be replaced by the approved wording before any real user
// sees this screen.
export const LOCATION_NOTICE_TEXT =
  "If you turn this on, this device's location is sent with your message so a search can start near you. " +
  "It is rounded to about 100 metres, it is used for that search only, and it is not stored. " +
  "Google Maps receives it to run the search. You can turn this off at any time.";

export function LocationControl({ location }: { location: LocationConsent }) {
  const { phase, problem } = location;
  return (
    <div className="location">
      {phase === "notice" || phase === "working" ? (
        <div className="notice" role="dialog" aria-labelledby="location-notice-title">
          <p id="location-notice-title" className="placeholder-flag">
            PLACEHOLDER NOTICE (location-notice-1), pending counsel review (issue #27)
          </p>
          <p>{LOCATION_NOTICE_TEXT}</p>
          <div className="notice-actions">
            <button type="button" onClick={() => void location.accept()} disabled={phase === "working"}>
              {phase === "working" ? "Working..." : "Agree and use my location"}
            </button>
            <button type="button" className="secondary" onClick={location.cancel} disabled={phase === "working"}>
              Cancel
            </button>
          </div>
        </div>
      ) : phase === "on" ? (
        <div className="location-on">
          <span role="status">Using your location for searches.</span>
          <button type="button" className="secondary" onClick={() => void location.stop()}>
            Stop using my location
          </button>
        </div>
      ) : (
        <button type="button" className="secondary" onClick={location.start} disabled={phase === "unknown"}>
          Use my location
        </button>
      )}
      {problem ? (
        <p className="notice notice-error" role="alert">
          {problem}
        </p>
      ) : null}
    </div>
  );
}
