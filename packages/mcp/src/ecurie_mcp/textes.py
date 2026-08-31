"""Ce que les outils disent d'eux-mêmes, en anglais.

Ce fichier ne contient que de la rédaction, et c'en est quand même : la
description d'un outil est lue par un modèle qui choisit, pas par un humain qui
apprend. Chaque mot est payé par tous les appels de toutes les sessions, et le
mot qui manque coûte un aller-retour — un outil mal départagé de son voisin se
paie en jobs lancés pour rien.

Trois règles gouvernent ce qui est écrit ici, et elles se vérifient :

- **aucun nom de modèle.** Écurie choisit le variant, et il change d'une machine
  à l'autre : une description qui nomme ses poids ment dès le premier `pull` ;
- **aucune promesse de qualité.** « high-quality », « accurate », « state of the
  art » sont des mots vides pour un sélecteur d'outil, et ce projet ne promet que
  ce qu'il a mesuré ;
- **les clés de `champs` sont celles du contrat, à la lettre.** Un champ inventé
  serait démenti par le schéma au premier appel ; un champ omis laisse l'agent
  deviner. Un test le vérifie contre `registry/capabilities/`.

Les contrats, eux, gardent leur français : ils parlent à l'Atelier et à qui écrit
un manifeste. C'est la surface produit qui migre à l'anglais, et elle d'abord —
décision 3 du pivot.
"""

from ecurie_mcp.catalogue import CATALOGUE_OUTIL, RUN_OUTIL, STATUS_OUTIL

# Les douze. Chaque entrée porte `title`, `description` et `champs` — un par
# champ du bloc `input` du contrat.
TEXTES: dict[str, dict] = {
    "speech-to-text": {
        "title": "Transcribe speech to text",
        "description": (
            "Transcribe what is said in a recording — an interview, a meeting, a voice memo. "
            "Words only, never who said them; timestamps alone name no speaker, so use "
            "speaker_diarization for turns and both for a labelled transcript. Returns absolute "
            "paths to a transcript and timestamped segments, plus the language retained; short "
            "text comes back inline."
        ),
        "champs": {
            "audio": "Absolute path to an audio file (any audio/* container) to transcribe.",
            "language": (
                "Expected spoken language, from the codes the worker announces on load; omit for "
                "automatic detection."
            ),
            "task": (
                "transcribe keeps the spoken language, translate renders English; default "
                "transcribe, and not every variant serves translate."
            ),
            "word_timestamps": (
                "Timestamp every word instead of every segment; default false, and markedly "
                "slower."
            ),
            "beam_size": "Decoding beam width, 1 to 8, default 5; past 5 the gain is marginal.",
            "temperature": (
                "0 to 1, default 0 for deterministic decoding; raise only to break a repetition "
                "loop."
            ),
        },
    },
    "speaker-diarization": {
        "title": "Who spoke when",
        "description": (
            "Split a recording into speaker turns — who talks, from when to when, and how many "
            "distinct voices. Transcribes nothing: pair it with speech_to_text when both words "
            "and speakers are wanted. e.g. {\"audio\": \"/abs/meeting.wav\", \"num_speakers\": "
            "3}. Returns absolute paths to JSON turns and an RTTM file, plus the speaker count."
        ),
        "champs": {
            "audio": "Absolute path to the audio/* recording to split into turns.",
            "num_speakers": (
                "Expected speaker count, 0 to 16, default 0 = let the model decide; set it when "
                "known."
            ),
            "threshold": (
                "Per-frame speech decision, 0 to 1, default 0.5; higher drops interruptions, "
                "lower makes silence speak."
            ),
            "min_segment_seconds": (
                "Turns shorter than this, 0 to 10 seconds, default 0.5, are merged into their "
                "neighbour."
            ),
            "max_seconds": (
                "Seconds processed from the start, 1 to 7200, default 1800; the rest is ignored."
            ),
        },
    },
    "audio-separation": {
        "title": "Split a mix into stems",
        "description": (
            "Pull a musical mixture apart into stems — vocals and accompaniment, or vocals, "
            "drums, bass and other — for an instrumental or an a cappella. Assumes studio music, "
            "not a voice in noise, and returns no words: feed its vocals track to speech_to_text "
            "for those. Returns one absolute WAV path per stem."
        ),
        "champs": {
            "audio": (
                "Absolute path to the mixed audio/* file to separate; trained on studio music, "
                "weaker on field recordings."
            ),
            "stems": (
                "2 for vocals plus accompaniment, 4 for vocals, drums, bass and other; default 2, "
                "same cost either way."
            ),
            "shifts": (
                "Shifted passes averaged together, 1 to 10, default 1; runtime multiplies by this "
                "count."
            ),
            "segment_seconds": (
                "Length of the chunks processed, 1 to 60 seconds, default 10; lower it on a long "
                "track."
            ),
        },
    },
    "text-to-speech": {
        "title": "Speak text aloud",
        "description": (
            "Speak a text aloud with one of the voices the engine ships — a voiceover, an audio "
            "note, a read-back. Imitating a specific person from a reference sample is a separate "
            "contract. Writes a WAV file and returns its absolute path plus a resource link, "
            "never audio bytes."
        ),
        "champs": {
            "text": "Text to read aloud, at least one character; sent to the engine verbatim.",
            "voice": (
                "Voice id, from the set the worker announces on load; omit for the variant's "
                "default."
            ),
            "speed": "Speaking-rate multiplier, 0.5 to 2.0, default 1.0; 2.0 doubles the pace.",
            "seed": (
                "Random seed, 0 or more. These engines sample: without it, two runs of the same "
                "text differ."
            ),
        },
    },
    "image-to-text": {
        "title": "Describe or question an image",
        "description": (
            "Say what is in a picture, or answer a question about it — a screenshot, a chart. "
            "Reading printed or handwritten text word for word is OCR: run the document-to-text "
            "contract through ecurie_run instead. e.g. {\"image\": \"/abs/shot.png\", "
            "\"question\": \"what error is shown?\"}. Short answers come back inline, longer ones "
            "as an absolute file path."
        ),
        "champs": {
            "image": "Absolute path to the image/* file to look at.",
            "question": "Question asked about the image; omit for a free description.",
            "detail": (
                "Answer length: bref (one sentence), normal (default), détaillé (planes and "
                "objects enumerated)."
            ),
            "language": (
                "Answer language, from the codes the worker announces on load; omit to follow the "
                "question, French otherwise."
            ),
            "max_tokens": (
                "Cap on tokens produced, 16 to 4096, default 512; it, not the image, sets job "
                "duration."
            ),
            "temperature": "0 to 2, default 0.2; higher invents detail the image does not contain.",
            "seed": (
                "Random seed, 0 or more; without it answers vary whenever temperature is "
                "non-zero."
            ),
        },
    },
    "depth-estimation": {
        "title": "Per-pixel distance map",
        "description": (
            "Estimate how far every pixel sits from the camera, from a single photo. Feeds "
            "background blur by distance, cut-out by depth, view alignment, or a step before "
            "meshing. Depth is relative, not metric. Returns absolute paths to a 16-bit depth "
            "map, a readable preview and camera intrinsics, plus near and far bounds."
        ),
        "champs": {
            "image": "Absolute path to the image/* file to estimate depth for.",
            "process_res": (
                "Side of the reasoning grid, 256 to 2048, default 504; output is rendered at this "
                "resolution, not the input's."
            ),
            "colormap": (
                "Preview palette: turbo (default), magma or gris; it changes nothing in the depth "
                "values."
            ),
        },
    },
    "image-segment": {
        "title": "Segment what you designate",
        "description": (
            "Cut out the object you designate — by a word, by points, or by a box; at least one "
            "is required. Use image_matting when the model should pick the subject itself. e.g. "
            "{\"image\": \"/abs/street.jpg\", \"prompt\": \"the dog\"}. Returns absolute paths to "
            "the chosen mask, an overlay and the runner-up masks, plus its score."
        ),
        "champs": {
            "image": "Absolute path to the image/* file to segment.",
            "prompt": (
                "Concept to cut out, in words, max 200 characters; catches every matching "
                "instance, and not every variant serves it."
            ),
            "points": (
                "Up to 32 points, each {x, y, include}, in original-image pixels; include "
                "defaults true, false excludes an area."
            ),
            "box": "Bounding box {x1, y1, x2, y2} in original-image pixels; combines with points.",
            "max_side": (
                "Largest input side tolerated, 256 to 4096 pixels, default 2048; give coordinates "
                "in original-image pixels regardless."
            ),
        },
    },
    "image-matting": {
        "title": "Cut out the subject",
        "description": (
            "Lift the main subject off its background and return it on transparency — remove the "
            "background of a photo, prepare a sticker. The model picks the subject itself; use "
            "image_segment to cut something you name or point at. Returns absolute paths to an "
            "RGBA cutout and to the alpha mask alone."
        ),
        "champs": {
            "image": "Absolute path to the image/* file whose subject to isolate.",
            "edge_refine": (
                "Refine edges after the first mask, default true; helps hair and foliage, barely "
                "manufactured objects."
            ),
            "threshold": (
                "Binarize the alpha at this level, 0 to 1; no default — omit for continuous "
                "alpha, usually wanted."
            ),
            "max_side": (
                "Largest side given to the model, 256 to 4096 pixels, default 1024; the mask is "
                "rescaled to the original."
            ),
        },
    },
    "text-to-image": {
        "title": "Generate an image",
        "description": (
            "Generate a picture from a written description, starting from noise — a cover, an "
            "illustration, a mock-up. Use image_to_image to rework a picture you already have, "
            "image_upscale to enlarge one without redrawing it. Writes a PNG and returns its "
            "absolute path; cost tracks width × height × steps."
        ),
        "champs": {
            "prompt": "What the image should show, in words; must be non-empty.",
            "negative_prompt": (
                "What to keep out of the image; no effect on distilled-guidance variants."
            ),
            "width": "Width in pixels, 256 to 2048, multiple of 64, default 1024.",
            "height": "Height in pixels, 256 to 2048, multiple of 64, default 1024.",
            "steps": "Denoising steps, 1 to 100; beyond 50 the gain is rarely visible.",
            "guidance_scale": (
                "Adherence to the prompt, 0 to 20; distilled variants work near 1."
            ),
            "seed": "Random seed, 0 or more; keep it to reproduce the same image.",
        },
    },
    "image-to-image": {
        "title": "Transform an existing image",
        "description": (
            "Redraw a picture you already have under a written instruction — restyle it, change a "
            "season, turn a photo into a drawing. No mask needed; strength decides how much "
            "survives. Use text_to_image when there is no source picture, image_upscale to "
            "enlarge without changing content. Returns the absolute path to a PNG."
        ),
        "champs": {
            "image": "Absolute path to the starting image/* file.",
            "prompt": "The transformation asked for, in words; must be non-empty.",
            "negative_prompt": "What to keep out; no effect on distilled-guidance variants.",
            "strength": (
                "Transformation force, 0 to 1, default 0.6: 0 keeps the source, 1 redraws it "
                "entirely."
            ),
            "steps": "Denoising steps applied to the transformed portion, 1 to 100.",
            "guidance_scale": "Adherence to the transformation instruction, 0 to 20.",
            "max_side": (
                "Largest input side tolerated, 256 to 2048 pixels, default 1024; a raw phone "
                "photo must be capped here."
            ),
            "seed": "Random seed, 0 or more; keep it to replay the same transformation.",
        },
    },
    "image-upscale": {
        "title": "Enlarge an image",
        "description": (
            "Raise the resolution of a picture without changing what it shows — a small logo, a "
            "1024-wide generation, a scan to print. Adds plausible detail, invents no object. Use "
            "image_to_image to actually alter the content, text_to_image to create one. Returns "
            "the absolute path to the enlarged PNG with the width and height obtained."
        ),
        "champs": {
            "image": "Absolute path to the image/* file to enlarge.",
            "scale": (
                "Enlargement factor 2, 3 or 4, default 2; cost follows the square, and some "
                "variants serve only their trained factor."
            ),
            "denoise": (
                "Denoising before enlargement, 0 to 1; no default — omit for the variant's own. "
                "High values flatten fine texture."
            ),
            "max_side": (
                "Largest output side, 256 to 8192 pixels; width and height may be "
                "capped by it."
            ),
        },
    },
    "time-series-forecast": {
        "title": "Forecast a numeric series",
        "description": (
            "Extend observed numeric series over a horizon — sales, load, temperature — returning "
            "a fan of quantiles, not one line: the median is expected, the spread is what is "
            "unknown. Zero-shot; explains no cause, flags no anomaly, fills no gap. e.g. "
            "{\"serie\": \"/abs/sales.csv\", \"horizon\": 168}. Returns absolute CSV, JSON and "
            "plot paths."
        ),
        "champs": {
            "serie": (
                "Absolute path to a long-format CSV, one row per (series, timestamp); comma "
                "separator, dot decimal."
            ),
            "colonne_serie": (
                "Name of the series-id column, default item_id; name it even for a single series."
            ),
            "colonne_horodatage": (
                "Name of the timestamp column, default timestamp; any form pandas parses, ISO "
                "8601 always."
            ),
            "colonne_valeur": (
                "Name of the numeric column to extend, default target; a text column is refused."
            ),
            "horizon": (
                "Steps to forecast, 1 to 1024, default 24, in the series' own step; past 1024 the "
                "model loops."
            ),
            "contexte": (
                "Past steps read from the end, 64 to 8192, default 8192 — the maximum; 512 to "
                "8192 quadruples latency."
            ),
            "quantiles": (
                "1 to 21 levels between 0.01 and 0.99, default [0.1, 0.25, 0.5, 0.75, 0.9]; "
                "values outside are clamped, and the result names the levels kept."
            ),
            "freq": (
                "Pandas frequency alias (h, D, 15min), default empty = inferred; it never closes "
                "a gap."
            ),
            "covariables_futures": (
                "Absolute path to an optional CSV covering exactly the horizon; each covariate "
                "must also appear in serie."
            ),
            "graphique": (
                "Plot the fan over the tail of the history, default true; returned as a PNG path."
            ),
        },
    },
}

META_TEXTES: dict[str, dict] = {
    CATALOGUE_OUTIL: {
        "title": "Browse capabilities",
        "description": (
            "List every capability this machine declares, whether each can run right now, "
            "and which have a dedicated tool in this session. Pass a capability id to get "
            "its input schema, its variants and their measured memory peaks — do that "
            "before calling ecurie_run on it."
        ),
    },
    RUN_OUTIL: {
        "title": "Run any capability",
        "description": (
            "Run a capability that has no dedicated tool, by its contract id. The escape "
            "hatch for the 29 experimental contracts: no maintenance promise, and the "
            "input schema is the contract's own. Read it with ecurie_catalog first, "
            'e.g. {"capability": "document-to-text", "input": {"document": "/path/scan.pdf"}}.'
        ),
    },
    STATUS_OUTIL: {
        "title": "Machine status",
        "description": (
            "Report what the machine holds: models currently resident and what each "
            "occupies, the Metal memory budget and how it was obtained, and disk "
            "accounting. Read-only — nothing here loads, unloads or deletes anything."
        ),
    },
}
