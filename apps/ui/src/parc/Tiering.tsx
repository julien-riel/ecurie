/**
 * Le tiering : où déporter, ce qui l'est déjà, et ce qui pèse le plus lourd.
 *
 * Trois blocs, dans l'ordre de la décision qu'on prend ici — **ai-je un volume,
 * qu'ai-je déjà déporté, que déporterais-je ensuite**. Comme le plan de GC,
 * l'écran ne déclenche rien : un déport copie des giga-octets, relit la copie
 * pour la prouver et met l'original en quarantaine, puis demande de committer un
 * `tier: cold` dans le manifeste. La dernière moitié n'est pas automatisable — le
 * §4.4 de la conception dit que l'outil ne touche pas au registre, toute
 * évolution du parc passant par Git —, si bien qu'un bouton dans un navigateur
 * laisserait le manifeste mentir sur l'état du disque jusqu'au prochain commit.
 *
 * Deux chiffres par variant, et leur écart est le sujet : `bytes` est ce qu'il
 * occupe, `freed_bytes` ce que le volume de départ récupérerait. Un fichier
 * qu'un lien dur retient ailleurs pèse ses giga-octets et n'en rend aucun ;
 * n'afficher que le premier ferait déporter pour rien.
 */

import type { ColdLink, TierVolume, VariantFootprint } from "../api/types";
import { formatOctetsDisque } from "../format/bytes";
import { phraseVolume } from "./parc";

/** Ce qu'on montre sans déplier : au-delà, un parc complet fait vingt-six lignes. */
const PREMIERS_VARIANTS = 8;

export interface TieringProps {
  volumes: TierVolume[];
  cold: ColdLink[];
  variants: VariantFootprint[];
}

export function Tiering({ volumes, cold, variants }: TieringProps) {
  const déportables = variants.filter((v) => v.tierable);
  const premierVolume = volumes.find((v) => v.mounted)?.path ?? "/Volumes/Parc";

  return (
    <section className="ecurie-carte">
      <p className="ecurie-plaque">ce qu'on pourrait mettre au pré</p>
      <h2>Tiering</h2>
      <p className="ecurie-etat-champ">
        Déporter copie sur un volume externe, prouve la copie, met l'original en quarantaine et
        laisse un lien. Le manifeste n'est pas modifié : la commande affiche le{" "}
        <code>tier: cold</code> à committer.
      </p>

      <h3>Volumes déclarés</h3>
      {volumes.length === 0 ? (
        <p className="ecurie-etat-champ">
          Aucun <code>tier_volumes</code> dans <code>~/.ecurie/config.toml</code> — le déclarer fait
          scanner le volume externe et signale les variants froids quand il est démonté.
        </p>
      ) : (
        <ul className="ecurie-volumes">
          {volumes.map((volume) => (
            <li key={volume.path} data-monte={volume.mounted ? "oui" : "non"}>
              <code>{volume.path}</code> <span>{phraseVolume(volume)}</span>
            </li>
          ))}
        </ul>
      )}

      <h3>Variants déportés ({cold.length})</h3>
      {cold.length === 0 ? (
        <p className="ecurie-etat-champ">Aucun variant n'est déporté : tout tient sur le disque.</p>
      ) : (
        <table className="ecurie-table">
          <thead>
            <tr>
              <th scope="col">Lien</th>
              <th scope="col">Cible</th>
              <th scope="col">État</th>
            </tr>
          </thead>
          <tbody>
            {cold.map((lien) => (
              <tr key={lien.path} data-disponible={lien.available ? "oui" : "non"}>
                <th scope="row">
                  <code>{lien.path}</code>
                  {lien.variant_ref ? (
                    <span className="ecurie-etat-champ"> {lien.variant_ref}</span>
                  ) : null}
                </th>
                <td>
                  <code>{lien.target}</code>
                </td>
                <td>{lien.available ? "disponible" : "volume absent"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>Ce qui pèse le plus lourd</h3>
      {déportables.length === 0 ? (
        <p className="ecurie-etat-champ">
          Aucun variant à déporter — tout est déjà parti, ou aucun fichier observé n'est rattaché à
          un variant du registre.
        </p>
      ) : (
        <>
          <table className="ecurie-table">
            <thead>
              <tr>
                <th scope="col">Variant</th>
                <th scope="col">Occupe</th>
                <th scope="col">Rendrait</th>
                <th scope="col">Fichiers</th>
                <th scope="col">Déporter</th>
              </tr>
            </thead>
            <tbody>
              {déportables.slice(0, PREMIERS_VARIANTS).map((variant) => (
                <Ligne key={variant.ref} variant={variant} volume={premierVolume} />
              ))}
            </tbody>
          </table>
          {déportables.length > PREMIERS_VARIANTS ? (
            <details>
              <summary>{déportables.length - PREMIERS_VARIANTS} variant(s) de plus</summary>
              <table className="ecurie-table">
                <tbody>
                  {déportables.slice(PREMIERS_VARIANTS).map((variant) => (
                    <Ligne key={variant.ref} variant={variant} volume={premierVolume} />
                  ))}
                </tbody>
              </table>
            </details>
          ) : null}
        </>
      )}

      {variants.some((v) => !v.tierable && v.devices.length > 1) ? (
        <p className="text-danger">
          {variants
            .filter((v) => !v.tierable && v.devices.length > 1)
            .map((v) => v.ref)
            .join(", ")}{" "}
          : fichiers répartis sur plusieurs volumes — <code>ecurie store tier</code> ne gère pas ce
          cas.
        </p>
      ) : null}
    </section>
  );
}

function Ligne({ variant, volume }: { variant: VariantFootprint; volume: string }) {
  // Un écart entre ce qu'il occupe et ce qu'il rendrait n'a qu'une cause, et
  // elle décide : un lien dur tenu hors du parc scanné retient les octets, si
  // bien que le déport copierait sans rien libérer.
  const retenu = variant.freed_bytes < variant.bytes;
  return (
    <tr>
      <th scope="row">
        <code>{variant.ref}</code>
        {variant.shared_with.length ? (
          <span className="ecurie-etat-champ">
            {" "}
            poids partagés avec {variant.shared_with.join(", ")}
          </span>
        ) : null}
      </th>
      <td>{formatOctetsDisque(variant.bytes)}</td>
      <td data-retenu={retenu ? "oui" : "non"}>
        {formatOctetsDisque(variant.freed_bytes)}
        {retenu ? (
          <span className="ecurie-etat-champ"> un lien dur hors du parc retient le reste</span>
        ) : null}
      </td>
      <td>{variant.files}</td>
      <td>
        <code className="ecurie-commande">
          ecurie store tier {variant.ref} {volume}
        </code>
      </td>
    </tr>
  );
}
