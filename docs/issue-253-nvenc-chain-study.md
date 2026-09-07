# Pulsar #253 — NVENC, réception et décodage

> Historical increment: “without publication” describes the state when this
> study was written. Its code is included in 3.0.0, but ready-drain and async
> output remain experimental and disabled by default. See
> [current libobs defaults](LIBOBS-CHANGES.md#defaults-experiments-and-rollback).

## Résultat de cet incrément

Travail direct dans la conversation, sans agent ni publication. Le patch 0051
ajoute un prototype de vidage des paquets NVENC prêts sur le thread encodeur
existant. Il conserve les B-frames, la taille du pool, le preset, le débit et les
outils de compression. Il reste désactivé par défaut
(`PULSAR_NVENC_READY_DRAIN=0`). Il impose une reconstruction cohérente de libobs
et des modules : une fonction facultative est ajoutée à `obs_encoder_info`.

Le vidage peut avancer l'arrivée du paquet portant le premier contenu changé,
mais les essais ne démontrent pas d'amélioration de la première image décodée.
Le décodage NVDEC/CUVID explicitement testé est nettement plus lent dans ce
récepteur FFmpeg local ; le décodage logiciel reste la référence par défaut.
Aucun noyau CUDA, pilote, firmware ou paramètre de qualité n'a été modifié.

## Hypothèses testées et contrat conservé

| Hypothèse | Changement | Limite de la preuve |
|---|---|---|
| Une sortie prête attend inutilement un nouvel appel encode | Callback facultatif `get_pending_packet`, vidage borné à 64 paquets, même thread et recherche des timings après leur enregistrement | Test natif de trois images à PTS/DTS réordonnés ; essais runtime H.264 D3D11 ; aucune généralisation CUDA, HEVC ou AV1 |
| Le récepteur peut utiliser le décodeur matériel plus vite | Option explicite `--rtmp-decoder nvdec-lowdelay`, CUVID et `low_delay`, un thread | Séquence/pixels vérifiés sur un clip puis mesures en direct ; ne représente pas un décodeur zéro-copie personnalisé |
| La mesure de pixels est parfois perdue dans les logs | Crop 32×32 interne au repère 64×64, `signalstats` puis métadonnées AVIO directes | Mesure à réception de la troisième moyenne de plan, pas à l'affichage ; aucune moyenne manquante inventée |

`NV_ENC_ERR_NEED_MORE_INPUT` n'autorise aucun vidage. Le préfixe prêt est publié
sur `NV_ENC_SUCCESS`, comme dans l'intégration FFmpeg consultée. Les paquets
gardent leur PTS/DTS ; aucune réduction des B-frames ni suppression de l'ordre
de présentation n'est utilisée pour fabriquer une baisse de latence.

Le chemin GPU NVENC est déjà alimenté par textures D3D11. Supprimer sa copie
intermédiaire sans protocole de durée de vie serait incorrect : la texture
empruntée peut être réutilisée par le producteur alors que l'encodeur la retient.
Un chemin zéro-copie avec surfaces possédées/fences constitue un travail distinct,
non implémenté et sans gain mesuré ici. CUDA n'est donc pas ajouté par principe.

## Mesures

Hôte RTX 3070, Ryzen 7 3800X ; 1080p60, WGC et CEF visibles, Program audio et
enregistrement actifs, RTMP loopback. Chaque passe de performance contient
100 Takes de chauffe puis 100 Takes mesurés. Les quatre binaires principaux
sont hachés. La révision binaire est `5c5757e62c7c701057fdd3386808bee8c2c69288` ;
les scripts de chaque passe ont leurs hashes propres dans `run-settings.json`.

La première passe de métadonnées précède l'ajout de l'option NVDEC au script.
Les quatre passes logicielles suivantes et les deux NVDEC utilisent le même
hash de sonde. Ne pas présenter toutes les passes comme une révision de script
unique. La courte batterie Python de la dernière référence a été lancée pendant
la chauffe ; aucune compilation n'a été lancée pendant les prises mesurées.

| Mode | Raw p95 ms | Paquet sélectionné p95 ms | Premier repère décodé p95 ms |
|---|---:|---:|---:|
| Logiciel, vidage désactivé, passe 1 | 22,62 | 69,90 | 74,98 |
| Logiciel, vidage désactivé, passe 2 | 21,71 | 69,73 | 72,93 |
| Logiciel, vidage activé, première sonde métadonnées | 22,62 | 71,31 | 77,97 |
| Logiciel, vidage activé, sonde courante | 22,05 | 71,39 | 74,29 |
| Logiciel, vidage activé, sonde courante, passe 2 | 24,15 | 72,31 | 77,48 |
| NVDEC, vidage désactivé | 22,57 | 71,96 | 162,69 |
| NVDEC, vidage activé | 21,27 | 70,20 | 153,41 |

Ces valeurs sont des p95 par passe, jamais des percentiles regroupés. Le candidat
décodé sélectionné reste une autre borne : 133,53–143,85 ms pour les passes
logicielles ci-dessus. Le changement d'indicateur n'est pas un gain d'encodeur.

Le nouveau rapport sépare aussi l'arrivée au démux du **paquet propre au premier
repère**, et son observation décodée. Dans la sonde courante, le vidage donne
56,24 puis 67,45 ms p95 à l'arrivée de ce paquet contre 69,18–69,23 ms sans vidage.
Le premier essai métadonnées donnait 61,60 ms. L'avance de 13 ms de la première
passe n'est donc pas reproduite à cette amplitude : la seconde ne donne que
1,7–1,8 ms. Ce signal intermédiaire variable n'est pas un gain bout-en-bout :
le p95 démux→repère décodé reste proche de 50 ms en logiciel,
et la latence finale reste inchangée. La médiane de cette attente n'est que de
3–6 ms : ce n'est pas un retard fixe à retrancher des mesures. Avec NVDEC elle
monte à 83–97 ms en médiane et 123–130 ms au p95.

Les percentiles de segments ne s'additionnent pas. L'arrivée observée au démux
inclut la livraison du journal FFmpeg ; ce n'est pas un timestamp réseau brut.
L'observation décodée inclut les dépendances/réordonnancements du codec et la
livraison des métadonnées ; elle ne certifie pas l'instant exact de sortie du
décodeur ni l'affichage physique.

## Audit et essais rejetés

Sur un clip de 1 842 images, les sorties framemd5 logiciel et CUVID-lowdelay sont
identiques pour toutes les lignes de données : PTS, DTS, taille et MD5 des pixels.
Cela valide ce clip, pas une capacité générale ni une latence minimale. Les
images I/P/B sont conservées. Les fichiers de comparaison sont versionnés.

Un premier smoke a échoué parce que la fenêtre de test était minimisée. Elle a
été restaurée et les portes de contrôle des pixels ont été conservées. Un essai
de 100 Takes avec l'ancien observateur `showinfo` a échoué à la fusion avec des
moyennes de plans manquantes ; aucune latence de cet essai n'est retenue. Les
appels de journalisation fragmentés dans le code `showinfo` expliquent un risque
d'entrelacement, mais le fragment exact n'a pas été conservé pour chaque image
rejetée. Le passage aux métadonnées directes élimine cette source de fragilité
dans les essais suivants sans assouplir le classifieur.

Sources primaires consultées : [NVENC Programming Guide 13.0](https://docs.nvidia.com/video-technologies/video-codec-sdk/13.0/nvenc-video-encoder-api-prog-guide/index.html),
[intégration NVENC FFmpeg](https://github.com/FFmpeg/FFmpeg/blob/master/libavcodec/nvenc.c),
[CUVID FFmpeg n7.1](https://github.com/FFmpeg/FFmpeg/blob/n7.1/libavcodec/cuviddec.c),
[showinfo FFmpeg](https://github.com/FFmpeg/FFmpeg/blob/master/libavfilter/vf_showinfo.c).
Le catalogue de compétences NVIDIA a été consulté ; aucune compétence Windows
NVENC adaptée n'a été trouvée et aucune n'a été installée.

## Décision et limites restantes

Conserver le prototype pour les expériences, sans promotion par défaut. Le
débit maximal d'un encodeur et sa latence avec B-frames sont des propriétés
différentes : ces résultats ne prouvent pas que NVENC soit moins performant que
x264 à qualité/configuration égales. Ils ne prouvent pas non plus un minimum
physique atteint.

La capacité sous surcharge, la qualité à débit égal, la phase A/V flash/clic et
un récepteur/réseau externe ne sont pas qualifiés par cette campagne. Le seuil
callback→récepteur de 15 ms p95 reste dépassé dans quatre des sept passes de la
table. La comparaison de capacité sans référence single-lane reste `UNPROVEN`.
Aucun résultat de sonde réussi ne remplace ces verdicts.

Rollback : laisser le vidage à 0 et le décodeur sur `software`. Le retrait du
patch 0051 exige une reconstruction cohérente du fork et des modules. Aucun
merge, push, déploiement ou changement distant n'est inclus.

## Validation et livraison

- Construction complète headless/CEF et reconstruction du prototype réussies.
  Les hashes des quatre binaires restent identiques après la batterie finale.
- Python : 197 réussis, 1 ignoré ; les refus d'identité, d'ordre, de métadonnées
  incomplètes et de mode de décodage divergent restent actifs.
- CTest complet avec vidage activé : 19/20 réussis. Le seul échec est
  `pulsar-dir-hardening-probe` : deux gestes de propriétaires Windows ne peuvent
  être exécutés sans `SeRestorePrivilege`. Le refus est conservé, pas maquillé.
- Programme audio réel : 100 Cuts NVENC et 100 Cuts x264 réussis, AAC 48 kHz,
  341 et 345 paquets audio respectivement, PTS continus, identité/routage stables
  et modification Preview isolée. Ce n'est pas une mesure flash/clic de phase A/V.
- Sept passes de performance de la table : 700 Takes mesurés après 700 de chauffe,
  chaque première image certifiée par son repère et sa séquence décodée.

Synthèse machine : `evidence/253/eleven/20260907T143100Z-complete-study.json`.
Archive des onze dossiers d'essais, y compris ceux rejetés :
`evidence/253/eleven/20260907T143300Z-nvenc-run-evidence.zip` ; 121 fichiers sans
vidéos, tous relus et vérifiés par SHA256 contre les originaux. Hash de l'archive :
`186C7B550D4F0C90679B36FDCDADC095C96335D67FB288FFC8F74600E43059AE`.
Les originaux avec vidéos sont livrés séparément dans
`D:/Documents/Zab/Artifacts/2026-09-07/pulsar-253-nvenc-chain/`. Les anciens
chemins absolus dans les journaux désignent ces fichiers avant relocalisation.
La clé HMAC privée n'est pas exportée ; les traces restent également vérifiables
localement avec la clé protégée déjà conservée dans le dépôt canonique.
