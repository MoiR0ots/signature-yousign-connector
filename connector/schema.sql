CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    demande TEXT NOT NULL,
    statut_esup TEXT NOT NULL,
    numero_etape TEXT NOT NULL,
    acteur TEXT NOT NULL,
    commentaire TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'received',
    received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (demande, statut_esup, numero_etape, acteur, commentaire)
);

CREATE TABLE IF NOT EXISTS observations (
    event_id INTEGER PRIMARY KEY REFERENCES events(id),
    observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    etat TEXT NOT NULL,
    motif TEXT,
    detail TEXT,
    id_etape INTEGER,
    numero_etape INTEGER,
    type_signature TEXT,
    toutes_signatures_requises INTEGER,
    document_pdf TEXT
);

-- La relecture d'un événement remplace son observation et ses emplacements.
CREATE TABLE IF NOT EXISTS destinataires_observes (
    event_id INTEGER NOT NULL REFERENCES observations(event_id) ON DELETE CASCADE,
    id_destinataire INTEGER NOT NULL,
    prenom TEXT, nom TEXT, email TEXT, type TEXT, deja_signe INTEGER,
    PRIMARY KEY (event_id, id_destinataire)
);

CREATE TABLE IF NOT EXISTS emplacements (
    event_id INTEGER NOT NULL,
    id_destinataire INTEGER NOT NULL,
    numero INTEGER NOT NULL,
    document INTEGER, page INTEGER, x REAL, y REAL, largeur REAL, hauteur REAL,
    rotation REAL, echelle REAL, toutes_les_pages INTEGER, champ_pdf TEXT,
    PRIMARY KEY (event_id, id_destinataire, numero),
    FOREIGN KEY (event_id, id_destinataire)
        REFERENCES destinataires_observes(event_id, id_destinataire) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS brouillons_yousign (
    parapheur TEXT NOT NULL,
    etape TEXT NOT NULL,
    id_demande_yousign TEXT,
    en_cours TEXT NOT NULL,
    id_destinataire_en_cours TEXT,
    pret INTEGER NOT NULL DEFAULT 0,
    activation_bloquee INTEGER NOT NULL DEFAULT 0,
    erreur TEXT,
    statut_yousign TEXT,
    active_le TEXT,
    termine_le TEXT,
    PRIMARY KEY (parapheur, etape)
);

-- Les positions suivent l'ordre des demandes dans ESUP, à partir de zéro.
CREATE TABLE IF NOT EXISTS documents (
    parapheur TEXT NOT NULL,
    etape TEXT NOT NULL,
    demande TEXT NOT NULL,
    position INTEGER NOT NULL,
    empreinte_contexte TEXT NOT NULL,
    id_document_source INTEGER NOT NULL,
    sha256_source TEXT NOT NULL,
    id_document_yousign TEXT,
    document_signe TEXT,
    id_document_retour INTEGER,
    statut_demande_retour TEXT,
    statut_parapheur_retour TEXT,
    retour_doublon INTEGER,
    PRIMARY KEY (demande, etape),
    UNIQUE (parapheur, etape, position),
    FOREIGN KEY (parapheur, etape) REFERENCES brouillons_yousign(parapheur, etape)
);

CREATE TABLE IF NOT EXISTS signataires (
    parapheur TEXT NOT NULL,
    etape TEXT NOT NULL,
    id_destinataire_esup TEXT NOT NULL,
    id_signataire_yousign TEXT NOT NULL,
    statut_yousign TEXT,
    lien_signature TEXT,
    PRIMARY KEY (parapheur, etape, id_destinataire_esup),
    FOREIGN KEY (parapheur, etape) REFERENCES brouillons_yousign(parapheur, etape)
);

CREATE TABLE IF NOT EXISTS configuration_yousign (
    id INTEGER PRIMARY KEY, api_url TEXT NOT NULL
);

CREATE VIEW IF NOT EXISTS dossiers_a_verifier AS
WITH derniers AS (SELECT demande, max(id) AS id FROM events GROUP BY demande)
SELECT e.demande, b.parapheur, e.id AS evenement, e.numero_etape, o.id_etape AS id_etape_esup,
       b.id_demande_yousign AS id_yousign,
       CASE WHEN b.parapheur IS NULL THEN 'avant envoi' ELSE 'préparation' END AS phase,
       CASE WHEN b.parapheur IS NULL THEN 'Relecture ESUP après 5 minutes'
            ELSE 'Vérification manuelle' END AS reprise,
       coalesce(o.detail, o.motif, 'Consulter le journal') AS motif,
       CASE WHEN b.parapheur IS NULL THEN
           datetime(coalesce(o.observed_at, e.received_at), '+5 minutes') END AS prochain_essai_utc
FROM derniers d JOIN events e ON e.id=d.id
LEFT JOIN observations o ON o.event_id=e.id
LEFT JOIN documents doc ON doc.demande=e.demande
    AND (o.id_etape IS NULL OR doc.etape=CAST(o.id_etape AS TEXT))
LEFT JOIN brouillons_yousign b ON b.parapheur=doc.parapheur AND b.etape=doc.etape
WHERE e.state IN ('erreur', 'erreur_yousign') AND coalesce(b.en_cours, '')=''
      AND NOT coalesce(b.activation_bloquee, 0) AND b.erreur IS NULL
UNION ALL
SELECT NULL, parapheur, NULL, NULL, etape, id_demande_yousign,
       CASE WHEN activation_bloquee THEN 'activation'
            WHEN en_cours != '' THEN en_cours ELSE 'suivi' END,
       CASE WHEN activation_bloquee OR en_cours != '' THEN 'Vérification manuelle' ELSE 'Au prochain cycle' END,
       coalesce(erreur, 'Vérifier le résultat de la préparation dans Yousign'), NULL
FROM brouillons_yousign WHERE en_cours != '' OR activation_bloquee OR erreur IS NOT NULL;
