// Paquet de ressources français — locale par défaut (NFR-10).
// Le français est la langue officielle du Togo ; l'architecture i18next permet
// d'ajouter d'autres langues ultérieurement sans toucher aux composants.
const fr = {
  translation: {
    app: {
      title: "Centres médicaux du Togo",
      tagline: "Trouvez un centre médical près de chez vous.",
    },
    nav: {
      map: "Carte",
      directory: "Annuaire",
      account: "Compte",
    },
    pages: {
      map: {
        title: "Carte du Togo",
        placeholder:
          "La carte interactive des régions sera disponible prochainement.",
        ariaLabel: "Carte interactive des régions du Togo",
        intro: "Touchez une région pour afficher les centres médicaux.",
        loading: "Chargement de la carte…",
        loadError: "Impossible de charger la carte des régions.",
      },
      // Repli statique quand WebGL/carte est indisponible (tâche 15.3, exigence 7.5).
      regionFallback: {
        ariaLabel: "Sélecteur de région (liste de repli)",
        intro: "Sélectionnez une région pour afficher les centres médicaux.",
        selectRegion: "Région {{region}}",
      },
      // Feuille inférieure listant les centres d'une région (tâche 15.2).
      centerSheet: {
        title: "Centres de la région {{region}}",
        close: "Fermer",
        loading: "Chargement des centres…",
        loadError: "Impossible de charger les centres de cette région.",
        retry: "Réessayer",
        empty: "Aucun centre médical publié dans cette région pour le moment.",
        count_one: "{{count}} centre",
        count_other: "{{count}} centres",
        viewProfile: "Voir le profil du centre {{name}}",
        noAddress: "Adresse non renseignée",
      },
      // Annuaire public : parcourir/filtrer les centres par région (tâche 16).
      directory: {
        title: "Annuaire des centres",
        intro: "Choisissez une région pour afficher ses centres médicaux.",
        allRegions: "Toutes les régions",
        loadingRegions: "Chargement des régions…",
        regionsError: "Impossible de charger les régions.",
        loadingCenters: "Chargement des centres…",
        centersError: "Impossible de charger les centres de cette région.",
        retry: "Réessayer",
        empty: "Aucun centre médical publié dans cette région pour le moment.",
        emptyAll: "Aucun centre médical publié pour le moment.",
        count_one: "{{count}} centre",
        count_other: "{{count}} centres",
        viewProfile: "Voir le profil du centre {{name}}",
        noAddress: "Adresse non renseignée",
        regionOption: "{{region}} ({{count}})",
      },
      // Profil public d'un centre (tâche 16, exigence 7.4).
      centerProfile: {
        title: "Profil du centre",
        back: "Retour",
        loading: "Chargement du profil…",
        loadError: "Impossible de charger le profil de ce centre.",
        notFound: "Ce centre est introuvable ou n'est plus disponible.",
        retry: "Réessayer",
        regionLabel: "Région",
        addressLabel: "Adresse",
        contactLabel: "Contact",
        phoneLabel: "Téléphone",
        emailLabel: "Courriel",
        hoursLabel: "Horaires d'ouverture",
        servicesLabel: "Services proposés",
        coordinatesLabel: "Coordonnées",
        noAddress: "Adresse non renseignée",
        noContact: "Contact non renseigné",
        noHours: "Horaires non renseignés",
        noServices: "Services non renseignés",
        noCoordinates: "Coordonnées non renseignées",
        callAria: "Appeler le centre au {{phone}}",
        emailAria: "Envoyer un courriel à {{email}}",
      },
      // Éditeur du profil du centre (tâche 17.2) — REQ-6.1, 3.2, P7.
      centerAdmin: {
        title: "Profil de mon centre",
        // Session absente/verrouillée ou rôle sans tenant : pas d'accès.
        signInRequired:
          "Connectez-vous à votre centre pour gérer son profil.",
        // Rôles cadrés au tenant hors tenant_admin : consultation seule (6.1).
        readOnly:
          "Vous consultez le profil de votre centre. Seul l'administrateur "
          + "du centre peut le modifier.",
        loading: "Chargement du profil…",
        loadError: "Impossible de charger le profil de votre centre.",
        // Cas « ouverte déjà hors-ligne » (#4, L1) : le GET du profil échoue
        // faute de réseau ; on distingue cette absence de connexion d'une vraie
        // erreur serveur (loadError), affichée à la place de celle-ci.
        offlineLoad:
          "Vous êtes hors ligne. Une connexion Internet est nécessaire pour "
          + "consulter le profil de votre centre.",
        notFound:
          "Aucun profil de centre n'est associé à votre compte pour le moment.",
        retry: "Réessayer",
        nameLabel: "Nom du centre",
        regionLabel: "Région",
        addressLabel: "Adresse",
        phoneLabel: "Téléphone",
        emailLabel: "Courriel",
        locationLabel: "Localisation sur la carte",
        locationHint:
          "Touchez la carte pour placer l'épingle, ou faites-la glisser. "
          + "Le point doit se situer au Togo.",
        noLocation: "Aucune position définie",
        hoursLabel: "Horaires d'ouverture",
        openAria: "Heure d'ouverture le {{day}}",
        closeAria: "Heure de fermeture le {{day}}",
        servicesLabel: "Services proposés",
        noServices: "Aucun service renseigné",
        servicePlaceholder: "Ajouter un service (p. ex. maternité)",
        addService: "Ajouter",
        removeServiceAria: "Retirer le service {{service}}",
        save: "Enregistrer",
        saving: "Enregistrement…",
        saved: "Profil enregistré.",
        // Signalement a priori hors-ligne du profil (#4, ADR-1 / ADR G-6) :
        // le profil du centre est géré via un endpoint REST en ligne, donc non
        // modifiable hors-ligne. `offlineBanner` annonce l'indisponibilité,
        // `offlineHint` distingue le profil (en ligne) des PHI patients/visites
        // (local-first, utilisables hors-ligne), et `offlineSaveHint` justifie
        // le bouton « Enregistrer » désactivé. Voir aussi `offlineLoad` plus
        // haut (chargement déjà hors-ligne) et `errors.offline` (filet si la
        // connexion tombe pendant l'enregistrement).
        offlineBanner:
          "Vous êtes hors ligne. La modification du profil de votre centre "
          + "nécessite une connexion Internet.",
        offlineHint:
          "Les patients et les visites restent utilisables hors ligne : "
          + "ils se synchronisent dès le retour du réseau. Le profil du centre, "
          + "lui, est géré en ligne ; il ne peut donc pas être modifié tant que "
          + "vous êtes hors ligne.",
        offlineSaveHint:
          "Enregistrement indisponible hors ligne. Reconnectez-vous à Internet "
          + "pour enregistrer vos modifications.",
        // Carte du sélecteur d'épingle.
        map: {
          ariaLabel: "Carte pour placer l'épingle du centre (Togo)",
          loading: "Chargement de la carte…",
        },
        // Contrat d'erreur de la mise à jour (PATCH /api/center/profile).
        errors: {
          // 422 : coordonnées hors de la frontière du Togo (P7) ou service inconnu.
          outsideTogo:
            "La position doit se situer à l'intérieur du Togo. "
            + "Déplacez l'épingle à l'intérieur des frontières.",
          forbidden:
            "Vous n'avez pas les droits pour modifier ce profil.",
          notFound:
            "Aucun profil de centre n'est associé à votre compte.",
          offline:
            "L'enregistrement nécessite une connexion Internet. "
            + "Vérifiez votre réseau et réessayez.",
          generic:
            "L'enregistrement a échoué. Réessayez dans quelques instants.",
        },
      },
      // CRUD Patients local-first (tâche 17.3) — REQ 4.1, 5.1, 5.6 ; P9.
      patients: {
        title: "Patients",
        signInRequired:
          "Connectez-vous à votre centre pour gérer les patients.",
        addPatient: "Nouveau patient",
        closeForm: "Fermer",
        newPatient: "Nouveau patient",
        searchPlaceholder: "Rechercher par nom ou identifiant…",
        loading: "Chargement des patients…",
        empty: "Aucun patient enregistré pour le moment.",
        mrnLabel: "Identifiant (MRN)",
        givenNameLabel: "Prénom",
        familyNameLabel: "Nom",
        phoneLabel: "Téléphone",
        // Consentement obligatoire au stockage des données (REQ-5.6).
        consentLabel:
          "Le patient consent au stockage et au traitement de ses données "
          + "de santé par le centre.",
        save: "Enregistrer",
        saving: "Enregistrement…",
        cancel: "Annuler",
        edit: "Modifier",
        delete: "Supprimer",
        editAria: "Modifier le patient {{name}}",
        deleteAria: "Supprimer le patient {{name}}",
        saveError:
          "L'enregistrement local a échoué. Réessayez dans quelques instants.",
      },
      // CRUD Visites local-first (tâche 17.3) — REQ 4.1, 5.1 ; P9.
      visits: {
        title: "Visites",
        signInRequired:
          "Connectez-vous à votre centre pour gérer les visites.",
        addVisit: "Nouvelle visite",
        closeForm: "Fermer",
        newVisit: "Nouvelle visite",
        noPatients:
          "Ajoutez d'abord un patient pour enregistrer une visite.",
        filterLabel: "Filtrer par patient",
        allPatients: "Tous les patients",
        patientLabel: "Patient",
        dateLabel: "Date de la visite",
        reasonLabel: "Motif",
        notesLabel: "Notes cliniques",
        noDate: "Date non renseignée",
        loading: "Chargement des visites…",
        empty: "Aucune visite enregistrée pour ce patient.",
        save: "Enregistrer",
        saving: "Enregistrement…",
        cancel: "Annuler",
        edit: "Modifier",
        delete: "Supprimer",
        editAria: "Modifier la visite",
        deleteAria: "Supprimer la visite",
        saveError:
          "L'enregistrement local a échoué. Réessayez dans quelques instants.",
      },
      // Connexion tenant + session locale (tâche 17.1) — REQ-3.2, NFR-7, NFR-8.
      account: {
        title: "Compte",
        placeholder: "La connexion sera disponible prochainement.",
        // Page de connexion (première connexion en ligne, puis session locale).
        login: {
          title: "Connexion",
          intro:
            "Connectez-vous pour accéder à votre centre. La première connexion "
            + "nécessite une connexion Internet ; ensuite, l'application "
            + "fonctionne hors ligne.",
          emailLabel: "Adresse e-mail",
          emailPlaceholder: "vous@centre.tg",
          passwordLabel: "Mot de passe",
          passwordPlaceholder: "Votre mot de passe",
          submit: "Se connecter",
          submitting: "Connexion en cours…",
          // 401 générique : ne révèle jamais si l'e-mail existe (P3).
          invalidCredentials: "E-mail ou mot de passe incorrect.",
          offlineError:
            "La première connexion nécessite une connexion Internet. "
            + "Vérifiez votre réseau et réessayez.",
          genericError:
            "La connexion a échoué. Réessayez dans quelques instants.",
          lockedError:
            "Trop de tentatives. Veuillez patienter avant de réessayer.",
          firstTime: "Première connexion avec un lien d'invitation ?",
          goToSetup: "Configurer mon mot de passe",
          // Session restaurée après rechargement : la base est verrouillée.
          unlockIntro:
            "Session active. Saisissez votre mot de passe pour déverrouiller "
            + "vos données locales.",
          unlockSubmit: "Déverrouiller",
        },
        // Session en cours (profil + déconnexion).
        session: {
          signedInAs: "Connecté·e en tant que",
          roleLabel: "Rôle",
          logout: "Se déconnecter",
          loggingOut: "Déconnexion…",
          locked: "Données locales verrouillées",
          unlocked: "Données locales déverrouillées",
          // Aire de travail tenant (tâches 17.2 / 17.3).
          workAreaLabel: "Espace de travail du centre",
          manageCenter: "Gérer le profil de mon centre",
          managePatients: "Patients",
          manageVisits: "Visites",
          backToAccount: "Retour au compte",
          roles: {
            platform_admin: "Administrateur de plateforme",
            tenant_admin: "Administrateur du centre",
            clinician: "Clinicien·ne",
            staff: "Personnel",
          },
        },
        // Page de configuration du mot de passe via jeton à usage unique (3.2).
        setCredentials: {
          title: "Configurer mon mot de passe",
          intro:
            "Saisissez le lien d'invitation reçu, puis choisissez un mot de "
            + "passe. Le lien est à usage unique et expire.",
          tokenLabel: "Jeton d'invitation",
          tokenPlaceholder: "st_…",
          passwordLabel: "Nouveau mot de passe",
          passwordPlaceholder: "Au moins 12 caractères",
          confirmLabel: "Confirmer le mot de passe",
          confirmPlaceholder: "Ressaisissez le mot de passe",
          submit: "Valider",
          submitting: "Validation en cours…",
          // Règles minimales alignées sur le backend (min_length=12).
          tooShort: "Le mot de passe doit contenir au moins 12 caractères.",
          mismatch: "Les deux mots de passe ne correspondent pas.",
          // 400/410 : jeton invalide, expiré ou déjà utilisé (sans fuite, P3).
          invalidToken:
            "Ce lien d'invitation est invalide, expiré ou déjà utilisé. "
            + "Demandez un nouveau lien à votre administrateur.",
          offlineError:
            "La configuration nécessite une connexion Internet. "
            + "Vérifiez votre réseau et réessayez.",
          genericError:
            "La configuration a échoué. Réessayez dans quelques instants.",
          success:
            "Mot de passe enregistré. Vous pouvez maintenant vous connecter.",
          goToLogin: "Aller à la connexion",
          backToLogin: "Retour à la connexion",
        },
      },
    },
    // Composants transversaux de l'aire tenant.
    components: {
      // Bandeau de conflits de synchronisation à revoir (tâche 17.3) — NFR-6, P12.
      conflictBanner: {
        title_one: "{{count}} conflit de synchronisation à revoir",
        title_other: "{{count}} conflits de synchronisation à revoir",
        // Les conflits sensibles (notes de visite) exigent une revue humaine.
        sensitiveTitle_one:
          "{{count}} conflit sensible à revoir",
        sensitiveTitle_other:
          "{{count}} conflits sensibles à revoir",
        subtitle:
          "Une donnée a été modifiée sur deux appareils. La version la plus "
          + "récente a été conservée ; vérifiez qu'aucune information n'a été perdue.",
        sensitiveTag: "Sensible",
        entities: {
          patient: "Patient",
          visit: "Visite",
        },
        winnerVersion: "Version conservée : {{version}}",
        showDetails: "Voir les détails",
        hideDetails: "Masquer les détails",
        acknowledge: "J'ai revu",
      },
    },
    // Barre d'état de synchronisation (NFR-5).
    sync: {
      online: "En ligne",
      offline: "Hors ligne",
      syncing: "Synchronisation…",
      synced: "Synchronisé",
      error: "Erreur de synchronisation",
      revoked: "Accès révoqué",
      pending_zero: "Aucun élément en attente",
      pending_one: "{{count}} élément en attente",
      pending_other: "{{count}} éléments en attente",
      conflicts_one: "{{count}} conflit à revoir",
      conflicts_other: "{{count}} conflits à revoir",
      retry: "Réessayer",
    },
    // Conventions togolaises de formatage (NFR-10).
    formats: {
      phonePrefix: "+228",
    },
  },
} as const;

export default fr;
