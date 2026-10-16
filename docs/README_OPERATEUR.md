# Installer et faire tourner PhotoBouche — guide de l'opérateur

Voilà de quoi monter le photobooth le soir même. Deux Raspberry Pi 4 :

- **raspi4-1** — le **gros** (32 Go). C'est le **booth** : il tient le Canon 5D, écoute
  le micro, affiche l'écran de contrôle, et sert les médias au diaporama. C'est aussi lui
  qui prépare les médias (voir § 4).
- **raspi4-2** — le **petit**. C'est le **diaporama** : il affiche le show sur le grand écran,
  avec le son, en tirant les photos et les clips depuis le gros Pi.

Rien n'a encore tourné sur le vrai matériel : prends ton temps, et si un truc coince,
note-le pour qu'on le règle.

Tout se fait sur la branche `main` du dépôt `sebabouche/photobouche`.

---

## 1. Le réseau (à faire en premier, c'est ce qui a bloqué la dernière fois)

Les deux Pi doivent être sur **le même Wi-Fi**, celui du lieu.

1. **raspi4-1 (le gros)** est déjà sur le Wi-Fi du lieu.
2. **raspi4-2 (le petit)** ne l'est probablement pas encore. Connecte-le **via la GUI** du
   bureau Pi : clique sur l'icône réseau en haut à droite, choisis le Wi-Fi du lieu,
   entre le mot de passe. (Au besoin, branche-lui un clavier/souris/écran le temps de le faire.)

**Trouver l'adresse du gros Pi.** Sur un Wi-Fi public, le nom `raspi4-1.local` ne marche
souvent pas (le mDNS est bloqué). Sur le gros Pi, ouvre un terminal et tape :

```sh
hostname -I        # affiche son IP, par ex. 10.0.0.42
```

Note cette IP : tu t'en serviras pour `booth_url` côté petit Pi (§ 6).

**Attention — l'isolation du Wi-Fi public.** Beaucoup de Wi-Fi publics empêchent deux
appareils de se parler entre eux (« isolation des clients »). Si, plus tard, le petit Pi
n'arrive pas à joindre le gros (le diaporama reste noir, `check-stream` échoue), c'est
très probablement ça. Teste depuis le petit Pi :

```sh
ping 10.0.0.42            # l'IP du gros Pi ; remplace par la vraie
```

Si le ping ne passe pas, le Wi-Fi public isole les clients. Il faudra alors un câble
Ethernet direct entre les deux Pi, ou un petit routeur dédié. Sans ça, le diaporama ne
pourra pas tirer les médias du booth.

---

## 2. Récupérer le logiciel (sur chaque Pi)

Sur **les deux** Pi, ouvre un terminal :

```sh
cd ~/photobouche      # s'il existe déjà
git pull
```

S'il n'existe pas encore, suis d'abord l'installation ci-dessous (§ 3 pour le gros,
§ 6 pour le petit).

---

## 3. Installer le gros Pi (booth) avec le 5D

```sh
sudo apt install python3-venv libportaudio2 gphoto2 exiv2 ffmpeg
# Le bureau Pi s'accapare l'appareil photo pour le gestionnaire de fichiers, ce qui bloque
# gphoto2 (« Could not claim the USB device ») : on désactive ce service.
systemctl --user mask --now gvfs-gphoto2-volume-monitor
sudo mkdir -p /srv/photobouche /etc/photobouche && sudo chown pi: /srv/photobouche
git clone https://github.com/sebabouche/photobouche.git ~/photobouche && cd ~/photobouche
python3 -m venv .venv && .venv/bin/pip install -e '.[booth]'
sudo cp config.example.toml /etc/photobouche/config.toml
cp deploy/photobouche-*.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now photobouche-camera photobouche-booth
sudo loginctl enable-linger pi
cp deploy/photobouche-screen-booth.desktop ~/.config/autostart/
.venv/bin/photobouche doctor
```

Le 5D :

- **Branche-le en USB** sur le gros Pi, allumé, carte CF dedans.
- Qualité **RAW + S1 JPEG**, auto power-off sur **Disable**, horloge à l'heure, **mise au
  point manuelle** (ou un point AF fixe) sur la bouche.
- **Ne lance jamais Darktable** sur le Pi : un seul programme peut tenir l'appareil.

`photobouche doctor` doit dire que l'appareil, le micro, mpv/gphoto2 et le dossier de données
sont OK. S'il râle, c'est le moment de régler avant la soirée.

---

## 4. Préparer les médias du making-of (sur le gros Pi)

Les photos et vidéos envoyées par tout le monde sont sur le cloud ; **Seb te donne le lien**
du dossier partagé.

1. **Télécharge** le contenu de ce dossier sur le gros Pi (via le navigateur du bureau, ou en
   téléchargeant le zip et en le décompressant).
2. Range-le sous `~/photobouche/external_data/`, **un dossier par personne** (garde les
   dossiers tels qu'ils sont sur le cloud). `external_data/` est ignoré par git : rien de tout
   ça ne part sur GitHub.
3. Prépare tout, directement dans le dossier que le booth sert (`/srv/photobouche`) :

```sh
cd ~/photobouche
.venv/bin/photobouche prepare external_data --dry-run      # regarde le plan d'abord
.venv/bin/photobouche prepare external_data /srv/photobouche   # écrit making_of/ et clips/
```

`prepare` enlève les doublons, met les photos en JPEG 1920 px, et **ré-encode tous les clips**
en H.264 que le Pi lit sans effort. C'est **long sur un Pi** (ffmpeg ré-encode une soixantaine
de clips) : laisse tourner, c'est normal. Le `--dry-run` t'annonce combien de photos et de
clips seront gardés.

> Comme le `prepare` écrit **directement dans `/srv/photobouche`** sur le gros Pi, tu n'as
> **pas** besoin de `push-media.sh` : ce script ne sert qu'à envoyer des médias préparés
> depuis un autre ordinateur. Ici tout est déjà sur place.

Les médias apparaissent dans le diaporama **en une seconde**, sans redémarrage.

---

## 5. Tester le booth

```sh
.venv/bin/photobouche mics                     # le micro USB doit apparaître ; note son nom
.venv/bin/photobouche listen "<nom du micro>"  # parle, crie : regarde le niveau et la ligne SHOOT
```

Mets le nom du micro dans `/etc/photobouche/config.toml`, ligne `mic = "..."`.

L'écran de contrôle (photo du haut + égaliseur en bas) démarre tout seul au boot (autostart).
Pour le lancer à la main :

```sh
.venv/bin/photobouche screen --profile booth
```

Déclenche une photo de trois façons, elles doivent toutes marcher :

- **la télécommande du 5D** (ou son déclencheur) — c'est l'usage normal ;
- **la touche Espace** sur l'écran de contrôle ;
- **un cri**, après avoir activé le mode cri avec la touche **S**.

La photo doit apparaître en ~2 s sur l'écran de contrôle. **Relève la latence** du
déclenchement USB qui s'affiche à chaque photo (attendu 0,3–0,6 s) : si c'est bien plus lent,
signale-le, il y a un repli matériel prévu.

Les touches de l'écran de contrôle : **X** cache la photo (elle part dans `rejected/`, jamais
supprimée), **S** mode cri, **M** dernière photo / cycle, **+ / −** seuil du cri, **Espace**
déclenche, **Échap** quitte.

---

## 6. Installer le petit Pi (diaporama)

```sh
sudo apt install python3-venv mpv
git clone https://github.com/sebabouche/photobouche.git ~/photobouche && cd ~/photobouche
python3 -m venv .venv && .venv/bin/pip install -e .
sudo mkdir -p /etc/photobouche && sudo cp config.example.toml /etc/photobouche/config.toml
cp deploy/photobouche-screen-show.desktop ~/.config/autostart/
```

Puis édite `/etc/photobouche/config.toml` et mets l'**IP du gros Pi** (relevée au § 1) dans
`booth_url`, pas `raspi4-1.local` :

```toml
booth_url = "http://10.0.0.42:8765"    # l'IP du gros Pi, avec le port 8765
```

Vérifie que le petit Pi voit bien le booth, puis mesure le débit :

```sh
.venv/bin/photobouche doctor
.venv/bin/photobouche check-stream     # par clip : débit depuis le booth vs débit du clip
.venv/bin/photobouche screen --profile show    # le vrai show ; --windowed pour une fenêtre
```

`check-stream` doit viser **×3 ou plus** pour chaque clip. **En dessous de ×1,5, le clip
va saccader** — c'est le risque du Wi-Fi public partagé. Si beaucoup de clips sont en dessous,
note-le : il faudra relier les deux Pi autrement (câble Ethernet direct), ou alléger les clips.

Si le booth devient injoignable pendant la soirée, le diaporama continue avec ce qu'il a déjà
en cache.

---

## 7. Si ça coince

- **Le diaporama reste noir / `check-stream` échoue** → le petit Pi ne joint pas le gros.
  Refais le `ping` du § 1. Neuf fois sur dix c'est l'isolation du Wi-Fi public.
- **`Could not claim the USB device`** → `gvfs-gphoto2-volume-monitor` n'est pas masqué (§ 3),
  ou Darktable tourne.
- **Le micro n'entend rien** (« micro muet ») → vérifie le nom dans `config.toml` avec
  `photobouche mics`, et relance `photobouche listen`.
- **Les clips saccadent** → `check-stream` est trop bas : Wi-Fi trop lent. Câble Ethernet
  entre les deux Pi.
- **Un doute** → `photobouche doctor` sur le Pi concerné dit ce qui manque.

Pour tout ça, tu peux demander à Claude Code sur le Pi : ouvre le dossier `~/photobouche`,
dis-lui que tu installes le booth ou le diaporama, il lance les commandes avec toi.
