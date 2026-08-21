/**
 * Registre 1 — un `x-ui` du contrat vers un widget RJSF.
 *
 * C'est l'une des deux tables d'aiguillage que la tâche 4.3 livre. Trois
 * propriétés la gouvernent, et chacune répond à un échec observé :
 *
 * **Elle est totale.** `widgetFor` ne lève jamais et ne rend jamais `undefined`
 * comme résultat : elle rend toujours une `Resolution`. Un `x-ui` inconnu
 * dégrade vers le widget déduit du type, avec un avis qui nomme le contrat, le
 * champ et la valeur. Planter priverait l'utilisateur de tous les autres champs
 * du formulaire à cause d'un seul ; ignorer en silence lui ferait croire que le
 * contrat a été honoré.
 *
 * **Elle couvre les cinq valeurs du méta-schéma**, pas les trois que la
 * conception nomme. `Record<XUi, WidgetEntry>` est ce qui le garantit : en
 * oublier une ne compile pas. `slider` et `hidden` ne sont employés par aucun
 * contrat aujourd'hui, et coûtent une ligne chacun parce que RJSF les fournit.
 *
 * **Elle décide dans un ordre écrit**, parce que plusieurs règles se disputent
 * le même champ : un `enum` et un `x-ui: select` ne demandent pas le même
 * widget, et c'est l'`enum` qui doit gagner (voir la règle 1).
 */

import type { RegistryWidgetsType } from "@rjsf/utils";
import type { Champ, XUi } from "../../schema/contract";
import { X_UI_CONNUS, accept, estChampFichier, estObjetLibre } from "../../schema/contract";
import type { Notice } from "../../notices/notices";
import { avisChamp } from "../../notices/notices";
import { FilePathWidget } from "./FilePathWidget";
import { OptionsWidget } from "./OptionsWidget";

/** Le contexte d'un champ au moment où l'on choisit son widget. */
export interface ChampCtx {
  /** Id du contrat, pour que tout avis nomme sa provenance. */
  capacite: string;
  /** Chemin pointé du champ, par exemple « tools.items.parameters ». */
  chemin: string;
  /** `x-options-from` déjà résolu. Peut être vide **légitimement**. */
  suggestions: readonly string[];
  /** Le variant choisi est-il chargé en mémoire ? Change la phrase, pas l'état. */
  resident: boolean;
}

export interface WidgetEntry {
  /** Clé du widget dans `WIDGETS`, ou dans le thème natif de RJSF. */
  readonly widget: string;
  /** Ce widget convient-il à ce champ ? Sinon on dégrade, avec un avis. */
  accepts(champ: Champ): boolean;
  /** Ce qui sera passé en `ui:options`. */
  options?(champ: Champ, ctx: ChampCtx): Record<string, unknown>;
}

export const CHEMIN_LOCAL = "chemin-local";
export const OPTIONS_RUNTIME = "options-runtime";

/**
 * Les cinq widgets du méta-schéma.
 *
 * `textarea`, `slider` et `hidden` sont ceux du thème HTML5 natif de RJSF :
 * `RangeWidget` dérive d'ailleurs son pas de `multipleOf` (mesuré : 64 pour
 * `text-to-image.width`), ce qu'un curseur écrit à la main aurait oublié.
 * `select` et `file`, eux, sont remplacés — pour des raisons que leurs fichiers
 * expliquent.
 */
export const X_UI_WIDGETS: Readonly<Record<XUi, WidgetEntry>> = {
  textarea: {
    widget: "textarea",
    accepts: (champ) => champ.type === "string",
  },
  select: {
    widget: OPTIONS_RUNTIME,
    accepts: (champ) => champ.type === "string",
    options: (_champ, ctx) => ({
      suggestions: ctx.suggestions,
      resident: ctx.resident,
    }),
  },
  file: {
    widget: CHEMIN_LOCAL,
    accepts: (champ) => champ.type === "string",
    options: (champ) => ({ accept: accept(champ.contentMediaType) }),
  },
  slider: {
    widget: "range",
    // Un curseur sans bornes n'a pas de course : RJSF rendrait un `<input
    // type="range">` de 0 à 100 quel que soit le champ.
    accepts: (champ) =>
      (champ.type === "number" || champ.type === "integer") &&
      champ.minimum !== undefined &&
      champ.maximum !== undefined,
  },
  hidden: {
    widget: "hidden",
    // Mesuré : la valeur reste dans l'entrée soumise, elle est seulement
    // invisible. C'est bien ce que le méta-schéma décrit.
    accepts: () => true,
  },
};

/** Ce que le compilateur d'`uiSchema` doit écrire pour un champ. */
export interface Resolution {
  /** `undefined` = RJSF déduit le widget du type, ce que le méta-schéma prévoit. */
  widget?: string;
  /** `ui:field`, seul moyen de rendre un objet libre — `ui:widget` y est ignoré. */
  field?: "json";
  options: Record<string, unknown>;
  notices: readonly Notice[];
}

function estXUiConnu(valeur: string): valeur is XUi {
  return (X_UI_CONNUS as readonly string[]).includes(valeur);
}

/**
 * Le widget d'un champ. Totale : ne lève jamais, rend toujours une résolution.
 *
 * L'ordre des règles est normatif.
 */
export function widgetFor(champ: Champ, ctx: ChampCtx): Resolution {
  const notices: Notice[] = [];
  const xui = champ["x-ui"];

  // 1. Un `enum` l'emporte sur tout widget imposé.
  //
  // Mesuré sur RJSF 6.8 : le `SelectWidget` natif encode ses options **par
  // index** et restitue donc le nombre 44100, non la chaîne « 44100 ». Un widget
  // écrit à la main qui lirait `event.target.value` enverrait une chaîne, que le
  // validateur Draft 2020-12 du serveur refuserait sur un champ `integer`.
  // Quatre champs du parc sont dans ce cas (`sample_rate`, `stems`,
  // `octree_resolution`, `scale`), et l'un des enums contient un accent.
  if (champ.enum) {
    if (xui !== undefined && xui !== "select") {
      notices.push(
        avisChamp(
          ctx.capacite,
          ctx.chemin,
          `x-ui « ${xui} » ignoré : ce champ déclare un enum, rendu en liste fermée.`,
        ),
      );
    }
    return { options: {}, notices };
  }

  // 2. Un JSON Schema arbitraire, que RJSF ne rend pas du tout.
  //
  // Avant les règles `x-ui`, et non après : le méta-schéma n'interdit pas de
  // poser un `x-ui` sur un champ de type objet, et le laisser sortir par les
  // règles 4 ou 5 lui retirerait son `ui:field` — donc son seul contrôle — tout
  // en affirmant dans l'avis qu'il est « rendu par le widget du type ». Un
  // champ requis rendu par un fieldset vide, avec une phrase qui prétend le
  // contraire, est le pire des deux échecs qu'on cherche à éviter.
  if (estObjetLibre(champ)) {
    if (xui !== undefined) {
      notices.push(
        avisChamp(
          ctx.capacite,
          ctx.chemin,
          `x-ui « ${xui} » ignoré : ce champ porte un schéma libre, rendu en éditeur JSON.`,
        ),
      );
    }
    return { field: "json", options: {}, notices };
  }

  // 3. Un champ fichier, reconnu comme le serveur le reconnaît.
  if (estChampFichier(champ)) {
    // Le garde de la table s'applique ici aussi : un champ fichier qui ne
    // serait pas une chaîne recevrait sinon un contrôle de saisie de chemin sur
    // un tableau ou un objet, en silence — alors que le même `x-ui: "file"`
    // passé par la règle 4 serait refusé avec un avis.
    if (X_UI_WIDGETS.file.accepts(champ)) {
      return {
        widget: CHEMIN_LOCAL,
        options: { accept: accept(champ.contentMediaType) },
        notices,
      };
    }
    notices.push(
      avisChamp(
        ctx.capacite,
        ctx.chemin,
        `champ de fichier de type ${champ.type} : un chemin est une chaîne, ` +
          "rendu par le widget du type.",
      ),
    );
    return { options: {}, notices };
  }

  // 4. Un `x-ui` connu et adapté.
  if (xui !== undefined && estXUiConnu(xui)) {
    const entree = X_UI_WIDGETS[xui];
    if (entree.accepts(champ)) {
      return {
        widget: entree.widget,
        options: entree.options?.(champ, ctx) ?? {},
        notices,
      };
    }
    // 5. Connu mais inadapté — un `slider` sans bornes, par exemple.
    notices.push(
      avisChamp(
        ctx.capacite,
        ctx.chemin,
        `x-ui « ${xui} » ne convient pas à un champ de type ${champ.type}` +
          `${xui === "slider" ? " sans minimum ni maximum" : ""} : rendu par le widget du type.`,
      ),
    );
    return { options: {}, notices };
  }

  // 6. Un `x-ui` que cette version de l'UI ne connaît pas.
  if (xui !== undefined) {
    notices.push(
      avisChamp(
        ctx.capacite,
        ctx.chemin,
        `x-ui « ${xui} » inconnu de cette version de l'UI : rendu par le widget du type ` +
          `${champ.type}. Les valeurs reconnues sont ${X_UI_CONNUS.join(", ")}.`,
      ),
    );
    return { options: {}, notices };
  }

  // 7. Rien à imposer : RJSF déduit du type, ce que le méta-schéma prévoit.
  return { options: {}, notices };
}

/** Les widgets propres au socle, passés à `<Form widgets=…>`. */
export const WIDGETS: RegistryWidgetsType = {
  [CHEMIN_LOCAL]: FilePathWidget,
  [OPTIONS_RUNTIME]: OptionsWidget,
};
