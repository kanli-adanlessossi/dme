# Requirements Document

## Introduction

Application web multi-tenant, pensée mobile-first **et local-first (fonctionnement
hors ligne)**, destinée aux centres médicaux des cinq régions administratives du
Togo (Afrique de l'Ouest). **Chaque centre médical est un tenant.** Le point
d'entrée public est une carte interactive et exacte du Togo avec des régions
cliquables ; la sélection d'une région révèle les centres médicaux qui s'y
trouvent. Les utilisateurs authentifiés travaillent dans le contexte d'un seul
tenant (leur centre médical), avec une isolation stricte des données entre
tenants.

**Contrainte de connectivité (déterminante).** La connectivité internet est
intermittente dans la plupart des établissements de santé en dehors de Lomé.
Toute solution exigeant une connexion permanente est inutilisable en pratique.
Par conséquent, l'application de travail des membres d'un tenant DOIT être
**local-first** : elle lit et écrit d'abord dans un stockage local au poste, puis
se **synchronise** avec le serveur central dès que le réseau est disponible,
sans jamais bloquer l'utilisateur pendant une coupure. La carte publique reste
servie en ligne mais met ses ressources en cache pour tolérer les coupures côté
personnel.

Les exigences sont regroupées par ordre de dépendance : **Fondations → Multi-tenant
et sécurité → Local-first et synchronisation → Domaine clinique → Expérience
publique → Exploitabilité.** Cet ordre correspond au découpage architectural (la
carte publique et les fonctionnalités cliniques reposent toutes deux sur la
fondation multi-tenant, sécurité et synchronisation) ainsi qu'au plan de mise en
œuvre décrit dans `tasks.md`.

### Faits de contexte (vérifiés)
- Le Togo compte **5 régions administratives** (du nord au sud) : **Savanes, Kara,
  Centrale, Plateaux, Maritime.** Le Grand Lomé est parfois traité comme une 6ᵉ
  unité opérationnelle ; la plateforme le modélise comme une sous-zone de la
  région Maritime et conserve 5 régions canoniques. Source : Regions of Togo
  (Wikipedia).
- Un jeu de données ouvert sous licence CC-BY-4.0 des limites administratives du
  Togo (5 régions, 40 préfectures) est disponible pour le GeoJSON de la carte :
  `open-admin-data/togo-administrative-divisions`.

## Glossary

- **Tenant** : un centre médical unique. Toutes les données opérationnelles
  appartiennent à exactement un tenant.
- **Région** : l'une des 5 régions administratives togolaises. Donnée de référence
  partagée, non rattachée à un tenant.
- **Administrateur de plateforme** : opérateur transversal qui intègre les tenants
  et gère les données de référence. Non rattaché à un tenant unique.
- **Membre d'un tenant** : utilisateur rattaché à un seul tenant. Rôles :
  `tenant_admin`, `clinician`, `staff` (accueil / réception).
- **PHI** (Protected Health Information) : toute donnée identifiant un patient ou
  de nature clinique (`patient`, `visit`). Jamais exposée sur les points d'accès
  publics.
- **RLS** (Row-Level Security) : mécanisme PostgreSQL qui applique l'isolation des
  tenants au niveau de la base de données.
- **Local-first** : modèle où l'application lit/écrit d'abord dans un stockage
  local au poste et reste pleinement opérationnelle sans réseau, la
  synchronisation vers le serveur se faisant en arrière-plan.
- **PWA** (Progressive Web App) : application web installable, dotée d'un service
  worker, capable de se charger et de fonctionner hors ligne.
- **Outbox** : file d'attente locale des mutations non encore synchronisées,
  rejouée vers le serveur à la reconnexion.
- **Delta-sync** : synchronisation incrémentale ne transférant que les
  changements survenus depuis le dernier curseur de synchronisation.
- **LWW** (Last-Write-Wins) : stratégie de résolution de conflits conservant la
  version à l'horodatage/version le plus récent, avec journalisation.

## Non-functional Requirements

### NFR-1 Isolation des tenants (critique pour la sécurité)
- Le système DOIT garantir qu'un membre d'un tenant ne peut jamais lire ou écrire
  des données appartenant à un autre tenant.
- L'isolation DOIT être appliquée au niveau de la base de données (Row-Level
  Security PostgreSQL), pas uniquement dans le code applicatif.

### NFR-2 Protection des données et sûreté des données de santé
- Tout le trafic DOIT passer en TLS.
- Les mots de passe DOIVENT être stockés avec un hachage moderne (Argon2id ou
  bcrypt).
- La connexion publique DOIT être protégée contre les attaques par force brute
  (limitation de débit et/ou verrouillage après plusieurs échecs).
- Les PHI NE DOIVENT jamais apparaître sur un point d'accès non authentifié.

### NFR-3 Auditabilité (transversale, établie tôt)
- Le système DOIT tenir une piste d'audit en ajout seul (append-only) qui
  enregistre au minimum : les événements d'authentification (succès/échec), la
  création et la consultation de PHI, ainsi que les actions transversales de
  l'administrateur de plateforme.
- Les enregistrements d'audit DOIVENT capturer l'acteur, le tenant, l'action,
  l'entité, l'horodatage et l'adresse IP source.
- Les enregistrements d'audit DOIVENT toujours être écrits et NE DOIVENT PAS être
  masqués par le contexte de tenant (ils ne sont pas soumis aux restrictions de
  lecture RLS par tenant pour l'administrateur de plateforme). Voir design §7.
- L'audit DOIT être distribué-compatible : les événements PHI survenus hors ligne
  DOIVENT être journalisés localement puis téléversés à la synchronisation.
  Chaque événement DOIT porter l'horodatage de l'événement (côté client) et, à la
  réception, l'horodatage serveur ; aucun événement PHI hors ligne NE DOIT être
  perdu lors de la synchronisation.

### NFR-4 Cycle de vie des données et droits des patients
- Les dossiers cliniques (`patient`, `visit`) DOIVENT utiliser la suppression
  logique (soft-delete) ; le système NE DOIT PAS supprimer physiquement des PHI
  lors des opérations normales.
- LORSQU'un tenant est désactivé ALORS ses données DOIVENT être conservées (non
  supprimées) et rendues inaccessibles aux accès publics et aux membres.
- Le système DOIT enregistrer, pour chaque dossier patient, si le consentement au
  stockage des données du patient a été recueilli.
- L'architecture DOIT permettre l'export et l'effacement des données par tenant
  comme opération future (le schéma DOIT porter les champs nécessaires pour cadrer
  de telles opérations).

### NFR-5 Fonctionnement hors ligne et local-first (déterminant)
- L'application de travail des membres d'un tenant DOIT être une PWA installable,
  capable de se charger et de fonctionner sans connexion internet après la
  première visite.
- Toutes les opérations cliniques courantes (créer/consulter/modifier patient et
  visite, consulter le profil du centre) DOIVENT fonctionner entièrement hors
  ligne, en lisant/écrivant dans un stockage local au poste.
- LORSQUE le réseau est indisponible ALORS l'application NE DOIT PAS bloquer
  l'utilisateur ni perdre de données ; les écritures DOIVENT être conservées
  localement et mises en file (outbox).
- L'application DOIT indiquer clairement l'état de connectivité et l'état de
  synchronisation (en ligne/hors ligne, nombre de changements en attente).

### NFR-6 Synchronisation et résolution de conflits
- LORSQUE la connectivité est rétablie ALORS le système DOIT synchroniser
  automatiquement les mutations locales en attente vers le serveur et récupérer
  les changements distants (delta-sync), sans intervention de l'utilisateur.
- La synchronisation DOIT être idempotente et reprenable : une sync interrompue
  (coupure en cours) NE DOIT PAS créer de doublons ni corrompre l'état.
- Les identifiants d'enregistrement DOIVENT être générés côté client (UUID) afin
  d'éviter les collisions de clés entre postes travaillant hors ligne.
- Chaque enregistrement synchronisable DOIT porter une version/horodatage
  permettant la détection et la résolution de conflits.
- La résolution de conflits par défaut DOIT être Last-Write-Wins avec
  journalisation ; les conflits sur des données cliniques sensibles DOIVENT être
  marqués pour revue humaine plutôt qu'écrasés silencieusement.

### NFR-7 Authentification tolérante à l'hors-ligne
- Un utilisateur déjà authentifié DOIT pouvoir continuer à travailler pendant une
  coupure réseau, via une session locale, sans réémission de jeton tant que
  celui-ci est valide.
- Les jetons d'accès DOIVENT avoir une durée de vie suffisante pour couvrir une
  journée de travail hors ligne typique, avec rafraîchissement opportuniste à la
  reconnexion.
- LORSQU'un tenant ou un utilisateur est désactivé côté serveur ALORS l'accès
  DOIT être révoqué au plus tard à la prochaine synchronisation réussie.

### NFR-8 Sécurité des données locales
- Les PHI stockées sur le poste (stockage local) DOIVENT être chiffrées au repos.
- LORSQU'un utilisateur se déconnecte ou que sa session expire ALORS les PHI
  locales en clair DOIVENT être rendues inaccessibles (clé verrouillée).
- La perte ou le vol d'un poste NE DOIT PAS exposer les PHI en clair sans la clé
  de l'utilisateur.

### NFR-9 Mobile-first
- L'interface DOIT être conçue d'abord pour les petits écrans (≥320px) puis
  enrichie progressivement pour tablette/ordinateur.
- Les actions principales DOIVENT être atteignables au pouce ; cibles tactiles
  ≥44px.

### NFR-10 Localisation
- La langue par défaut de l'interface DOIT être le **français** (langue officielle
  du Togo), l'architecture permettant l'ajout d'autres langues ultérieurement.
- Les dates, heures et numéros de téléphone DOIVENT suivre les conventions
  togolaises (+228).

### NFR-11 Résilience et performance
- Le premier affichage significatif de la carte publique DOIT viser < 3 s sur un
  mobile de milieu de gamme en 3G.
- La carte DOIT se dégrader gracieusement (sélecteur de région statique) si WebGL
  est indisponible.
- Après la première visite, le chargement de l'application de travail NE DOIT PAS
  dépendre du réseau (ressources servies depuis le cache du service worker).

### NFR-12 Expérience de développement locale
- L'ensemble de la pile DOIT pouvoir s'exécuter localement via Docker Compose
  (Postgres/PostGIS + backend + frontend), démarrable d'une seule commande.

### NFR-13 Contrat d'API et de synchronisation
- Le backend DOIT publier un contrat OpenAPI qui fait référence unique pour le
  frontend ; le contrat DOIT être défini avant la construction des
  fonctionnalités frontend qui en dépendent.
- Le contrat DOIT inclure le protocole de synchronisation (points d'accès push
  des mutations et pull des deltas, format des curseurs et des conflits).

## Requirements

Chaque exigence utilise des critères d'acceptation de style EARS. Les exigences
sont ordonnées par dépendance architecturale, et non par visibilité dans
l'interface.

### Groupe A — Multi-tenant et sécurité (fondation)

### Requirement 1: Authentification et contexte de tenant

**User Story:** En tant que membre d'un tenant, je veux me connecter et travailler
automatiquement dans les données de mon centre, afin de ne jamais voir les données
d'un autre centre.

#### Acceptance Criteria

1. LORSQU'un utilisateur s'authentifie ALORS le système DOIT émettre une
   session/un jeton portant le `tenant_id` et le rôle de l'utilisateur.
2. TANT QU'une requête est authentifiée LE système DOIT définir le contexte de
   tenant en base afin que la RLS restreigne toutes les requêtes à ce tenant.
3. SI une requête tente d'accéder à une ressource hors de son tenant ALORS le
   système DOIT répondre 404 (ressource) ou 403 (permission) et NE DOIT PAS
   divulguer l'existence de la ressource.
4. LORSQU'un administrateur de plateforme s'authentifie ALORS le système DOIT
   autoriser des opérations transversales explicites et auditées uniquement via
   les points d'accès d'administration.
5. LORSQUE des échecs de connexion répétés surviennent pour un compte ou une
   source ALORS le système DOIT limiter ou verrouiller temporairement les
   tentatives suivantes (NFR-2).
6. LORSQU'un utilisateur déjà authentifié perd la connexion ALORS il DOIT pouvoir
   continuer à travailler via une session locale tant que son jeton est valide,
   sans nouvelle authentification serveur (NFR-7).

### Requirement 2: Contrôle d'accès basé sur les rôles

**User Story:** En tant qu'administrateur de tenant, je veux des rôles pour mon
personnel, afin que les permissions correspondent aux responsabilités.

#### Acceptance Criteria

1. LE système DOIT prendre en charge au moins ces rôles : platform_admin,
   tenant_admin, clinician, staff.
2. LORSQU'un utilisateur n'a pas le rôle requis pour une action ALORS le système
   DOIT la refuser avec un 403.
3. Les affectations de rôle DOIVENT être cadrées au tenant de l'utilisateur (sauf
   platform_admin).

### Requirement 3: Intégration et cycle de vie des tenants (administrateur de plateforme)

**User Story:** En tant qu'administrateur de plateforme, je veux enregistrer et
gérer les centres médicaux en tant que tenants, afin de les intégrer et de les
retirer en toute sécurité.

#### Acceptance Criteria

1. LORSQU'un administrateur de plateforme crée un tenant ALORS le système DOIT
   exiger : nom du centre, région (parmi les 5), adresse physique, coordonnées
   géographiques et contact.
2. LORSQU'un tenant est créé ALORS le système DOIT créer un utilisateur
   administrateur de tenant initial et DOIT fournir un jeton de configuration
   d'identifiants à usage unique et expirant, plutôt qu'un mot de passe en clair.
3. LE système DOIT empêcher un non-administrateur-de-plateforme de créer ou de
   lister des tenants autres que le sien.
4. LORSQU'un tenant est désactivé ALORS ses données DOIVENT être conservées (état
   logique), masquées de l'annuaire public, et ses membres DOIVENT se voir refuser
   la connexion (NFR-4).
5. LORSQU'une action d'intégration ou de cycle de vie survient ALORS le système
   DOIT écrire un enregistrement d'audit (NFR-3).

### Groupe B — Local-first et synchronisation

### Requirement 4: Fonctionnement hors ligne et synchronisation

**User Story:** En tant que clinicien dans un centre à connectivité intermittente,
je veux continuer à enregistrer et consulter les dossiers sans réseau, afin que
mon travail ne soit jamais bloqué et que rien ne soit perdu.

#### Acceptance Criteria

1. LORSQUE l'application est ouverte sans réseau (après une première visite) ALORS
   elle DOIT se charger et permettre les opérations cliniques courantes depuis le
   stockage local (NFR-5).
2. LORSQU'un membre crée ou modifie un enregistrement hors ligne ALORS le système
   DOIT l'écrire localement, lui attribuer un identifiant UUID généré côté client,
   et le placer dans l'outbox (NFR-6).
3. LORSQUE la connectivité revient ALORS le système DOIT rejouer automatiquement
   l'outbox vers le serveur et récupérer les deltas distants, de façon idempotente
   et reprenable (NFR-6).
4. SI un même enregistrement a été modifié des deux côtés ALORS le système DOIT
   résoudre le conflit en Last-Write-Wins avec journalisation, et DOIT marquer les
   conflits cliniques sensibles pour revue humaine plutôt que de les écraser
   silencieusement (NFR-6).
5. LORSQUE des PHI sont stockées localement ALORS elles DOIVENT être chiffrées au
   repos et rendues inaccessibles après déconnexion/expiration (NFR-8).
6. LORSQUE des événements PHI surviennent hors ligne ALORS ils DOIVENT être
   journalisés dans un audit local puis téléversés à la synchronisation, sans
   perte (NFR-3).
7. L'application DOIT afficher l'état de connectivité et le nombre de changements
   en attente de synchronisation (NFR-5).

### Groupe C — Domaine clinique (isolé par tenant)

### Requirement 5: Amorce du domaine clinique de base (minimal, extensible)

**User Story:** En tant que clinicien, je veux des dossiers patients et visites de
base cadrés à mon centre, afin que l'ossature démontre de vraies données isolées
par tenant.

#### Acceptance Criteria

1. LE système DOIT fournir des entités `patient` et `visit` cadrées au tenant en
   tant que domaine d'amorce (CRUD), chacune portant un `tenant_id`.
2. LORSQU'un enregistrement cadré au tenant est créé ALORS le système DOIT y
   apposer automatiquement le `tenant_id` de l'appelant (et non depuis l'entrée
   client).
3. Les données patient et visite NE DOIVENT jamais apparaître sur un point d'accès
   public.
4. LE système DOIT imposer l'unicité d'un patient au sein d'un tenant via un
   identifiant local au tenant, et DOIT prendre en charge les patients sans
   identifiant national.
5. LORSQU'un patient ou une visite est retiré ALORS le système DOIT effectuer une
   suppression logique (NFR-4) ; les PHI NE DOIVENT PAS être supprimées
   physiquement par le CRUD normal.
6. LORSQU'un dossier patient est créé ALORS le système DOIT capturer un indicateur
   de consentement indiquant si le patient a consenti au stockage des données
   (NFR-4).
7. LORSQUE des PHI sont créées ou consultées ALORS le système DOIT écrire un
   enregistrement d'audit (NFR-3).
8. Les entités `patient` et `visit` DOIVENT porter les champs de synchronisation
   (UUID client, version/horodatage, `deleted_at`) requis par l'exigence 4.

> Remarque : il s'agit d'une amorce d'ossature, pas d'un DME complet. Les flux
> cliniques sont hors périmètre pour la v1.

### Requirement 6: Gestion du profil du centre médical

**User Story:** En tant qu'administrateur de tenant, je veux gérer le profil de mon
centre, afin que l'annuaire public et la carte restent exacts.

#### Acceptance Criteria

1. LORSQU'un administrateur de tenant modifie le profil du centre ALORS le système
   DOIT mettre à jour le nom, l'adresse, les coordonnées, le contact, les horaires
   d'ouverture et les services proposés.
2. LORSQUE les coordonnées changent ALORS la carte publique DOIT refléter le nouvel
   emplacement au prochain chargement.
3. LE système DOIT valider que les coordonnées se situent à l'intérieur de la
   frontière du Togo.

### Groupe D — Expérience publique

### Requirement 7: Carte interactive du Togo (page d'accueil publique)

**User Story:** En tant que visiteur, je veux voir une carte exacte du Togo avec
des régions cliquables, afin de trouver des centres médicaux près de chez moi.

#### Acceptance Criteria

1. LORSQUE la page d'accueil se charge ALORS le système DOIT afficher une carte
   géographiquement exacte du Togo montrant les 5 régions comme des zones
   distinctes et cliquables.
2. LORSQU'un visiteur touche/clique une région ALORS le système DOIT mettre en
   évidence cette région et afficher la liste des centres médicaux qui s'y
   trouvent.
3. LORSQU'une région n'a aucun centre médical enregistré ALORS le système DOIT
   afficher un message d'état vide plutôt qu'une erreur.
4. LORSQU'un visiteur sélectionne un centre médical dans la liste ALORS le système
   DOIT afficher le profil public de ce centre (nom, adresse, région, contact,
   services, coordonnées).
5. SI le rendu WebGL/carte est indisponible ALORS le système DOIT présenter un
   sélecteur de région statique (liste des 5 régions) en repli (NFR-11).
6. Les limites des régions de la carte DOIVENT provenir d'un jeu de données ouvert
   faisant autorité et DOIVENT correspondre aux 5 régions officielles.

### Requirement 8: Annuaire des régions et des centres médicaux

**User Story:** En tant que visiteur, je veux parcourir les centres médicaux par
région, afin d'en choisir un sans utiliser la carte.

#### Acceptance Criteria

1. LE système DOIT exposer un annuaire public, en lecture seule, des centres
   médicaux regroupés par région.
2. LORSQU'un visiteur filtre par région ALORS le système DOIT ne renvoyer que les
   centres actifs de cette région.
3. L'annuaire public NE DOIT exposer que les champs de profil non sensibles du
   centre (jamais de PHI ni de données opérationnelles internes).

### Groupe E — Exploitabilité

### Requirement 9: Exécution locale et données d'amorçage

**User Story:** En tant que développeur, je veux une seule commande pour tout
exécuter avec des données d'amorçage réalistes, afin de développer rapidement.

#### Acceptance Criteria

1. LORSQU'un développeur lance `docker compose up` ALORS Postgres/PostGIS, le
   backend et le frontend DOIVENT démarrer et être accessibles.
2. LE système DOIT fournir un amorçage insérant les 5 régions et au moins un centre
   médical d'exemple par région avec des coordonnées togolaises valides.
3. LE backend DOIT exposer un point de contrôle de santé (health-check) et une
   documentation d'API auto-générée (OpenAPI, selon NFR-13).

## Out of Scope (v1)

- Dossier médical électronique complet / codage clinique.
- Prise de rendez-vous, facturation, assurance.
- Applications mobiles natives (l'application est une appli web responsive
  mobile-first).
- Paiements et passerelles SMS/notifications (laissés comme points d'intégration).
- Exécution automatisée de l'export/effacement des données par tenant (prise en
  charge au niveau du schéma uniquement ; voir NFR-4).

## Assumptions

- « Mobile-first » signifie une application web responsive et installable (PWA),
  pas une application native iOS/Android.
- « Local-first » cible une PWA avec stockage local chiffré et synchronisation
  en arrière-plan ; la synchronisation s'appuie sur une bibliothèque éprouvée
  (p. ex. RxDB/PouchDB) plutôt que sur une couche maison.
- La granularité de synchronisation est au niveau de l'enregistrement
  (patient/visite), pas au niveau du champ ; LWW suffit pour la v1, les conflits
  cliniques sensibles étant marqués pour revue.
- Les postes de travail des centres sont partagés mais utilisés par du personnel
  authentifié ; le chiffrement local protège contre perte/vol, pas contre un
  utilisateur légitime malveillant.
- Les limites des régions proviennent d'un jeu de données ouvert et attribué ; le
  détail au niveau préfecture est optionnel pour la v1 (la cliquabilité au niveau
  région est requise).
- Le français est la locale par défaut.
- Le Togo n'a pas de réglementation nationale unique sur les données de santé
  appliquée ici ; la conception suit les bonnes pratiques générales de protection
  des données de santé (TLS, audit, suppression logique, capture du consentement)
  afin de pouvoir se rattacher ultérieurement à un régime spécifique.
