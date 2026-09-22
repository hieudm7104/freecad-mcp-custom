import { Component, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App.js";
import "./style.css";

/**
 * React unmounts the entire tree on an uncaught render error, leaving an empty
 * #root — a bare page in the body colour, indistinguishable from "the bundle
 * never loaded". Without this, the only report possible is "the UI vanished",
 * and the cause stays in a console nobody is looking at. Put it on screen.
 */
class Boundary extends Component<{ children: ReactNode }, { err: Error | null }> {
  state: { err: Error | null } = { err: null };

  static getDerivedStateFromError(err: Error) {
    return { err };
  }

  render() {
    const { err } = this.state;
    if (!err) return this.props.children;
    return (
      <div className="crash">
        <h2>The UI crashed</h2>
        <p className="dim">
          The chat and both viewports are still alive on the server — this is a
          rendering fault in the page. Copy the trace below when reporting it.
        </p>
        <pre>{err.stack ?? String(err)}</pre>
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
