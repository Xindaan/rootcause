/* T-0612: Banner oberhalb der Tabs, solange eine Automatik-Sperre gilt.
 *
 * Nach einem Notfall-Stopp gießt die Automatik auf den betroffenen Kanälen
 * erst wieder, wenn Andre sie hier freigibt. Je Sperre: Zonen im Klartext,
 * seit wann, und ein Knopf mit Rückfrage. Die Daten kommen aus dem
 * 30-s-Takt von App.tsx; nach einer Freigabe lädt App.tsx neu (kein
 * optimistisches Ausblenden, vgl. Lernlog 10.04.2026).
 *
 * Gate G3-1: ist der Sperrstatus unklar, erscheint das Banner auch ohne
 * Einträge (Regeln in `sperrBannerAnsicht`). Sonst stünde die Automatik
 * fail-closed still, ohne dass es im Dashboard zu sehen ist. */
import type { KanalSperre, Zone } from '../typen'
import { sperrBannerAnsicht, type SperrStand } from './kanal-sperre'
import './KanalSperrBanner.css'

interface Props {
  sperren: ReadonlyArray<KanalSperre>
  zonen: ReadonlyArray<Pick<Zone, 'zone_id' | 'name'>>
  /** Stand der Liste (aktuell, Abruf gescheitert, Backend kann nicht lesen). */
  stand: SperrStand
  /** Id der Sperre, deren Freigabe gerade läuft (Knopf gesperrt). */
  freigabeLaeuft: number | null
  onFreigeben: (sperreId: number, rueckfrage: string) => void
}

export function KanalSperrBanner({
  sperren, zonen, stand, freigabeLaeuft, onFreigeben,
}: Props) {
  const ansicht = sperrBannerAnsicht(sperren, zonen, stand)
  if (!ansicht.sichtbar) return null
  return (
    <section
      className={`kanal-sperr-banner${ansicht.dringend ? ' ksb-dringend' : ''}`}
      aria-labelledby="kanal-sperr-titel"
    >
      <h2 id="kanal-sperr-titel" className="ksb-titel">
        {ansicht.titel}
      </h2>
      {ansicht.eintraege.length > 0 && (
        <ul className="ksb-liste">
          {ansicht.eintraege.map(e => (
            <li key={e.id} className="ksb-eintrag">
              <span className="ksb-text">
                <strong>{e.zonen}</strong> · gesperrt seit {e.seit}
              </span>
              <button
                type="button"
                className="ksb-knopf"
                disabled={freigabeLaeuft === e.id}
                onClick={() => onFreigeben(e.id, e.rueckfrage)}
              >
                {freigabeLaeuft === e.id ? 'Wird freigegeben …' : e.knopf}
              </button>
            </li>
          ))}
        </ul>
      )}
      {ansicht.standHinweis && (
        <p className="ksb-stand" role="status">{ansicht.standHinweis}</p>
      )}
      <p className="ksb-hinweis">Manuelles Gießen bleibt möglich.</p>
    </section>
  )
}
