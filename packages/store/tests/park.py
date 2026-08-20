"""Constantes du faux parc de tests."""

SHA_LIVE = "1a" * 32  # blob vivant, dupliqué entre HF et Ollama (1000 o)
SHA_STALE = "2b" * 32  # blob HF d'une révision détachée (500 o)
SHA_HORPH = "3c" * 32  # blob HF orphelin (200 o)
SHA_CONF = "4d" * 32  # config Ollama référencée (300 o)
SHA_OORPH = "5e" * 32  # blob Ollama orphelin (100 o)
ETAG40 = "9f" * 20  # petit fichier HF non-LFS, etag 40 caractères (10 o)
REV_LIVE = "e" * 40
REV_STALE = "d" * 40

# Le dépôt du manifeste réel : le faux cache HF le porte pour que le résolveur
# rattache vraiment ses blobs à qwen3-tts-1.7b@8bit-mlx. Épinglé au v0.3 (3.5),
# à suivre si le manifeste change de dépôt.
HF_REPO = "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit"
