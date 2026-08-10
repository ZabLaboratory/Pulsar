# Runbook — Qualifier un échec de mise à l'antenne

**S'applique à** : un output Pulsar (le stream historique, une destination
multi-stream, ou le virtualcam) qui n'atteint pas `live`, ou qui décroche après y
être arrivé.
**Référence** : ADR-005 §3.4, §3.5, §3.6, §3.9 (issues #182 `OutputFailed`, #183
gestionnaire de journal, #184 surface de diagnostic, #185 clé de session, #186
`OutputAttemptSettled`).
**Objectif** : distinguer en moins d'une minute « le direct n'a pas démarré » de
« un composant optionnel a échoué », **sans ouvrir le fichier de journal**.

---

## Étape 0 — Avant tout diagnostic : mettre les journaux à l'abri

**Toujours en premier, même si l'incident semble déjà compris.** Le répertoire de
journaux tourne sur trois bornes cumulatives et non alternatives : 10 fichiers,
16 Mio, **et** 7 jours (ADR-005 §3.1). La borne temporelle est délibérée — elle
protège contre une clé de flux dormant indéfiniment dans un journal peu alimenté —
mais elle a pour contrepartie assumée qu'un incident encore ouvert au huitième
jour n'a plus de preuve si personne ne l'a sortie de la zone de purge. C'est la
seule étape de ce runbook qui n'attend pas.

```powershell
Copy-Item "$env:LOCALAPPDATA\Pulsar\logs\*" "<dossier d'incident hors zone de purge>"
```

Chemin réel : `PULSAR_LOG_DIR` si la variable est positionnée, sinon
`%LOCALAPPDATA%\Pulsar\logs\` (défaut, `log-handler.cpp:default_log_dir`). Le
contenu est déjà rédigé (secrets masqués, ADR-005 §3.2) : la copie ne demande
aucune précaution supplémentaire.

---

## Étape 0bis — Échec immédiat au boot, avant tout événement vendeur

Un cas se qualifie **avant** l'Étape 1 : si le processus Pulsar n'a jamais atteint la
sentinelle `PULSAR_READY` et que stderr porte ces deux lignes (durcissement N1, #201/PR
#209, ADR-005 §5 R10) :

```
pulsar-dir-hardening: <dir>\obs-websocket is owned by another account; refusing to trust or re-harden it (fail closed, #201 N1); <dir>\obs-websocket
pulsar-headless: could not create/verify a protected, owned <dir>\obs-websocket; refusing to boot (fail closed, #201 N1)
```

→ Un répertoire `obs-websocket/` préexistant, dans le répertoire d'installation
per-utilisateur de Pulsar, est **possédé par un compte local autre que celui qui exécute
`pulsar.exe`**. Depuis le durcissement N1, ce n'est plus une DACL simplement re-corrigée
au boot : la propriété du répertoire fait échouer la vérification de propriétaire
(`harden_directory_dacl`/`create_directory_hardened`) et le boot refuse en permanence de
démarrer, à chaque tentative, tant que le répertoire n'a pas changé de main (comportement
voulu, remède F3 — finding O4, clearance Bastion PR #209/issue #201).

Prism ne voit qu'un code de sortie non nul sans ce détail : c'est le stderr de Pulsar,
capturé avant `PULSAR_READY`, qui porte la cause exacte.

**Geste opérateur** : supprimer le répertoire `obs-websocket/` avec les privilèges
appropriés (compte propriétaire, ou administrateur) avant de relancer Pulsar — il sera
recréé au prochain boot avec la DACL protégée user-only, propriété du compte qui exécute
`pulsar.exe`.

---

## Étape 1 — Qualifier depuis le verdict, jamais depuis le fichier

Le fait qui répond à « le direct a-t-il démarré ? » n'est **jamais** dans le
journal en première intention : il est dans l'événement vendeur
`pulsar:OutputAttemptSettled` (ADR-005 §3.5, issue #186). Émis une fois
exactement par tentative et par destination, succès compris — décidable sans
fenêtre temporelle ni corrélation.

```
pulsar:OutputAttemptSettled reçu pour cette destination ?
│
├── outcome = "live"
│     → La tentative a réussi. Un décrochage ultérieur est un fait distinct,
│       voir Étape 1bis (pulsar:OutputFailed).
│
├── outcome = "failed"
│     → Lire reason_class. Table de la cause probable et du geste opérateur :
│       Étape 2 ci-dessous.
│
└── Jamais reçu pour cette destination
      → La tentative n'a jamais été réglée : le composant n'a probablement
        jamais démarré (arrêt demandé par le client avant tout résultat —
        RC7 — ou tentative jamais lancée). Vérifier que StartDestination /
        StartAllDestinations a bien été appelé, puis que le processus Pulsar
        est toujours vivant (GetDiagnostics, Étape 3).
```

## Étape 1bis — Décrochage en cours de diffusion : `pulsar:OutputFailed`

Une fois `live` atteint, seul `pulsar:OutputFailed` fait autorité pour un
décrochage (ADR-005 §3.5) — `OutputAttemptSettled` ne se réémet jamais pour une
tentative déjà réglée. Charge : `output`, `code` (`OBS_OUTPUT_*`), `last_error`,
`reason_class`, `session`.

**Cas sans aucun événement (nominal, pas une panne)** : un arrêt demandé par le
client ne produit aucun `pulsar:OutputFailed` (RC7). L'échec d'attache d'un
filtre optionnel non plus, mais une trace `WARN` reste présente dans le journal —
récupérable via l'Étape 3, jamais en pariant sur une lecture en direct du fichier.

## Étape 2 — Table `reason_class` → cause probable → geste opérateur

Jeu fermé de sept valeurs (ADR-005 §3.4, issue #182). `unknown` est une valeur
légitime et attendue, pas un échec du classement.

| `reason_class` | Cause probable | Geste opérateur |
|---|---|---|
| `auth_rejected` | L'ingest a refusé les identifiants — clé invalide ou révoquée. | Vérifier la clé enregistrée sur la destination (`GetDestinations`). Si elle est simplement fausse, la corriger. Si une fuite est suspectée, poser le geste délibéré de rotation — voir section *Rotation de clé* ci-dessous. |
| `ingest_unreachable` | Aucune connexion établie — DNS, routage, port, serveur injoignable. | Vérifier la connectivité réseau locale et le statut du service d'ingest cible, puis retenter `StartDestination` une fois la connectivité rétablie. |
| `ingest_dropped` | Connexion établie puis perdue (ou refusée juste après) avant/pendant la diffusion. | Vérifier la stabilité du lien réseau local et `last_error`. Une récurrence sur la même destination pointe vers l'ingest, pas vers Pulsar — retenter avant d'escalader. |
| `encoder_failed` | L'encodeur n'a pas démarré ou s'est arrêté en erreur. | Vérifier `GetVideoSettings` / `GetCapabilities` (l'identité d'encodeur est fixée au boot) et la disponibilité du matériel d'encodage (GPU). Joindre la preuve d'Étape 3 avant d'escalader. |
| `config_rejected` | libobs a refusé la configuration avant toute tentative réseau — jamais atteint le réseau. | Vérifier les champs de la destination (`kind`, `url`, `key`) via `GetDestinations` / `CreateDestination`, corriger, retenter. |
| `disconnected_local` | Output sans surface réseau (virtualcam) : tout arrêt anormal est local. | Vérifier l'état du processus local (`GetDiagnostics`), qu'aucun `StopLogFileWrite` ni arrêt local involontaire n'est en cause. |
| `unknown` | Aucune classe ne s'applique. `last_error` brut est joint tel quel. | Suivre intégralement la procédure de collecte de preuve (Étape 3) et joindre `last_error` brut à l'escalade — ne pas deviner de classe. |

---

## Étape 3 — Procédure de collecte de preuve

1. **Requête de diagnostic** — `GetDiagnostics` (ADR-005 §3.6.1, issue #184),
   vendeur `pulsar`, sur le proc handler. Rend le chemin du journal courant, les
   compteurs par niveau depuis le démarrage (`count_error`/`count_warn`/
   `count_info`/`count_debug`), l'état des outputs, et les `N` dernières lignes
   `WARN`/`ERROR` (`recent_warn_error_lines`) — servi depuis la mémoire, jamais
   une relecture du fichier. Refusée avec une erreur explicite (jamais une liste
   vide) si obs-websocket n'est pas lié à la boucle locale.
2. **Clé de session** — `PULSAR_SESSION <id>` (ADR-005 §3.3, issue #185),
   imprimée sur stdout au boot, juste avant la sentinelle `PULSAR_READY`. Porte
   chaque ligne de journal Pulsar et chaque événement vendeur `pulsar:*` pour
   toute la durée du processus. C'est la clé de jointure.
3. **Jointure avec le journal du consommateur** — le journal de Pulsar et le
   journal de session du consommateur Prism restent deux fichiers distincts (deux
   processus, deux cycles de vie) ; ils deviennent **joignables** sur cette clé
   de session, jamais fusionnés. Reconstruire la séquence côté opérateur en
   filtrant les deux journaux sur le même `session`.

---

## Rotation de clé — un geste opérateur délibéré, jamais une étape automatique

La rotation d'une clé de flux Twitch **interrompt la diffusion**. ADR-005 (R1) la
nomme explicitement comme la réponse d'incident attendue en cas de fuite ou de
compromission suspectée d'une clé — parce qu'une clé de flux ne tourne jamais
seule et reste, à ce titre, le pire cas des deux secrets journalisés. À l'inverse,
le mot de passe obs-websocket est régénéré à chaque démarrage : sa fuite est
bornée à la session, aucune rotation n'est nécessaire pour lui.

Ce runbook ne l'automatise à aucune étape : c'est un geste que l'opérateur pose
consciemment, sachant qu'il coupe l'antenne en cours, jamais un branchement
déclenché par un `reason_class` ou par ce document.

---

## Renvois

Depuis `docs/DEVELOPMENT.md`, section *Troubleshooting*.
