# terranota-public

Public files for the Terra Nota iOS app, served by GitHub Pages at
https://terranota.app.

- `index.html` and `assets/`: the website.
- `privacy/`: the privacy policy.
- `data/maps-index.json`: a search index of maps georeferenced with
  [Allmaps](https://allmaps.org), built daily from its open data (CC0). It
  lists only maps whose library's licence lets Terra Nota use them.

## Map index

https://terranota.app/data/maps-index.json

`maps` is a list of rows in `columns` order, sorted by ground area (most local
first):

| Column | |
|---|---|
| `id` | Allmaps map ID; the full georeference is at `mapURLPrefix` + `id` |
| `title` | From the IIIF manifest or canvas label; null when there isn't one |
| `provider` | Index into `providers`, or null |
| `service` | IIIF Image API base URL |
| `minLat`, `minLon`, `maxLat`, `maxLon` | Extent of the map's outline |
| `area` | Ground area covered, in m² |
