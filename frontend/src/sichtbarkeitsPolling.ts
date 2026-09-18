/* T-0460: Ersatz fuer nacktes setInterval bei Polling-Effekten. Pausiert,
 * solange der Tab im Hintergrund liegt (document.hidden), und holt beim
 * Zurueckkehren einmal sofort nach statt bis zum naechsten regulaeren Tick
 * zu warten. Bewusst eine normale Funktion, kein React-Hook (`use*`-Praefix
 * vermieden) -- sie wird aus bestehenden useEffect-Bodies heraus aufgerufen,
 * die schon eigene Deps/AbortController haben; ein echter Hook duerfte dort
 * nicht verschachtelt aufgerufen werden (Rules of Hooks). */
export function starteSichtbarkeitsIntervall(callback: () => void, delayMs: number): () => void {
  let id: number | undefined

  const stop = () => {
    if (id !== undefined) {
      window.clearInterval(id)
      id = undefined
    }
  }
  const start = () => {
    if (id !== undefined) return
    id = window.setInterval(callback, delayMs)
  }
  const handleVisibility = () => {
    if (document.hidden) {
      stop()
    } else {
      callback()
      start()
    }
  }

  if (!document.hidden) start()
  document.addEventListener('visibilitychange', handleVisibility)

  return () => {
    document.removeEventListener('visibilitychange', handleVisibility)
    stop()
  }
}
