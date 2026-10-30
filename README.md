# Champaign–Urbana rentals map

Public map: https://mali207illinois.github.io/cu-rentals-map/

The intended custom domain is `rent.mustafaali.org`. Its Namecheap DNS needs a CNAME record for host `rent` pointing to `mali207illinois.github.io` before GitHub Pages can use it. After DNS is in place, add a root-level `CNAME` file containing `rent.mustafaali.org`, set that custom domain in Pages, and enable HTTPS after the certificate is issued.

GitHub Pages serves `index.html`, `mtd-transit.js`, and the five JSON feeds from the repository root. The [refresh workflow](.github/workflows/refresh-listings.yml) runs daily at 09:17 UTC and can also be started manually under Actions. Each source scraper validates and atomically replaces its own feed; failures retain the previous file, while successfully updated feeds are committed and automatically republished by Pages. Check the workflow run's logs and failure status for source errors.

The displayed offers are public realtor snapshots, not guaranteed unit inventory. Follow each listing link to confirm current pricing and availability.
