/**
 * Le validateur du formulaire — même dialecte que le serveur, autre rôle.
 *
 * Le serveur valide avec `Draft202012Validator` ; le défaut d'AJV est draft-07.
 * Les deux dialectes s'accordent sur tout ce que les contrats du parc emploient
 * aujourd'hui — `exclusiveMinimum` y est un nombre dans les deux, contrairement
 * à draft-04 —, et l'écart porte sur des mots-clés qu'aucun contrat n'utilise
 * encore : `$dynamicRef`, `dependentRequired`, `prefixItems`, et la sémantique
 * d'annotation de `contentMediaType`. Ce n'est donc pas un défaut qu'on corrige,
 * c'est un écart qu'on refuse d'ouvrir : le méta-schéma des capacités déclare
 * `$schema: draft 2020-12`, le serveur valide dans ce dialecte, et deux
 * validateurs qui ne lisent pas le même dialecte finiraient par diverger sur un
 * contrat qu'on n'a pas encore écrit.
 *
 * Les deux validations ne font pas le même travail, et il ne faut pas les
 * confondre. Celle-ci est **locale et instantanée** : elle souligne le champ
 * fautif pendant la frappe, gratuitement. Celle du serveur est **l'autorité** :
 * c'est elle qui décide qu'un job part, et ses phrases — `input_errors`,
 * `reason`, `blockers` — sont affichées telles quelles, jamais reformulées ni
 * fusionnées avec celles d'AJV. Un test vérifie que les deux désignent les mêmes
 * champs fautifs ; il ne leur demande pas de dire la même phrase.
 *
 * `ajv-i18n` traduit les messages : sans lui, un dépôt intégralement français
 * afficherait « must have required property ».
 *
 * Les extensions `x-ui` et `x-options-from` traversent AJV sans bruit : le mode
 * strict est déjà désactivé par `@rjsf/validator-ajv8`, et les clés inconnues
 * d'un schéma ne sont pas des erreurs de validation.
 */

import { customizeValidator } from "@rjsf/validator-ajv8";
import Ajv2020 from "ajv/dist/2020";
import localizeFr from "ajv-i18n/localize/fr";

export const validator = customizeValidator(
  { AjvClass: Ajv2020 as never },
  localizeFr as never,
);
