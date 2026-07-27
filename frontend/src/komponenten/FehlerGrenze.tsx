/* Error Boundary: faengt Render-Exceptions in Kind-Komponenten ab. */

import { Component, type ErrorInfo, type ReactNode } from 'react'

interface Props {
  titel?: string
  children: ReactNode
}

interface State {
  fehler: Error | null
}

export class FehlerGrenze extends Component<Props, State> {
  state: State = { fehler: null }

  static getDerivedStateFromError(fehler: Error): State {
    return { fehler }
  }

  componentDidCatch(fehler: Error, info: ErrorInfo) {
    // Konsole ist die einzige Ablage — fuer produktive Diagnose reicht das,
    // da das Dashboard lokal laeuft. Stack landet im componentStack, den wir
    // mit ausgeben, damit man den React-Pfad nachvollziehen kann.
    console.error('FehlerGrenze:', fehler, info.componentStack)
  }

  reset = () => {
    this.setState({ fehler: null })
  }

  render() {
    if (this.state.fehler) {
      return (
        <div
          role="alert"
          style={{
            border: '1px solid var(--farbe-gefahr, #c44)',
            background: 'var(--bg-karte, #fff)',
            borderRadius: 10,
            padding: 16,
            margin: 16,
            color: 'var(--text-primaer, #222)',
          }}
        >
          <h3 style={{ marginTop: 0 }}>
            {this.props.titel ?? 'Anzeige fehlgeschlagen'}
          </h3>
          <p style={{ fontSize: 14 }}>
            {this.state.fehler.message || 'Unbekannter Render-Fehler.'}
          </p>
          <button type="button" onClick={this.reset}>
            Nochmal versuchen
          </button>
        </div>
      )
    }
    return this.props.children
  }
}
