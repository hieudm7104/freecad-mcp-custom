import { Component, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App.js";
import "./style.css";

/**
 * React unmounts the entire tree on an uncaught render error, leaving an empty
 * #root — a bare page in the body colour, indistinguishable from "the bundle
 * never loaded". Without this, the only report possible is "the UI vanished",
 * and the cause stays in a console nobody is looking at.
 *
 * It keeps *every* error, oldest first, rather than just the latest. Swapping
 * this fallback in unmounts App/Chat/Preview, React runs their effect cleanups
 * during that commit, and it routes anything those throw back here too
 * (`captureCommitPhaseError`) — so the last error to arrive is usually the
 * fallout, and a single-slot `state.err` shows that instead of the cause.
 */
class Boundary extends Component<{ children: ReactNode }, { crashed: boolean }> {
  state = { crashed: false };
  errors: Error[] = [];

  static getDerivedStateFromError() {
    return { crashed: true };
  }

  componentDidCatch(err: Error) {
    this.errors.push(err);
    this.forceUpdate();
  }

  render() {
    if (!this.state.crashed) return this.props.children;
    return (
      <div className="crash">
        <h2>The UI crashed</h2>
        <p className="dim">
          The chat and both viewports are still alive on the server — this is a
          rendering fault in the page. Copy everything below when reporting it;
          the first entry is the cause, later ones are usually its fallout.
        </p>
        {this.errors.map((e, i) => (
          <pre key={i}>
            {`#${i + 1} ${e.stack ?? String(e)}`}
          </pre>
        ))}
        <button type="button" onClick={() => location.reload()}>
          Reload
        </button>
      </div>
    );
  }
}

createRoot(document.getElementById("root")!).render(
  <Boundary>
    <App />
  </Boundary>,
);
