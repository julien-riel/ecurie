# Runtime `mlx-flashhead`

Sert `talking-head` avec SoulX-FlashHead Pro, par le portage MLX d'AI2Apps.

```bash
uv run ecurie env sync mlx-flashhead
python3 runtimes/mlx-flashhead/vendorer.py
uv run ecurie pull soulx-flashhead@pro-bf16
```

`env sync` ne suffit pas : le portage n'est publié sur aucun index et n'a pas de
`pyproject.toml`. `vendorer.py` copie le module `flashhead_mlx` du dépôt
github.com/Avdpro/ai2apps, au commit épinglé, sous `vendor/`, que le `.gitignore`
exclut. Il vérifie l'empreinte du code copié : le profil du manifeste a été mesuré
sur ce code-là. Le code est sous Apache-2.0 ; `vendor/LICENSE` en garde le texte.

Un dossier rangé ailleurs se déclare par `ECURIE_FLASHHEAD_VENDOR`.

Seule la variante Pro est servie. Lite embarque le VAE de LTX-Video 0.9, réservé à la
recherche par sa licence : voir le manifeste `registry/models/soulx-flashhead.yaml`.
