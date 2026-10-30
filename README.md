# Champaign–Urbana rentals map

Public map: https://rent.mustafaali.org/

Namecheap has a CNAME for host `rent` pointing to `mali207illinois.github.io`. The [Pages workflow](.github/workflows/deploy-pages.yml) publishes the public files from `main`, including the `CNAME` file for the custom domain.

GitHub Pages serves `index.html`, `mtd-transit.js`, and the five JSON feeds. The [refresh workflow](.github/workflows/refresh-listings.yml) runs daily at 09:17 UTC and can also be started manually under Actions. Each source scraper validates and atomically replaces its own feed; failures retain the previous file, while successfully updated feeds are committed and automatically republished by Pages. Check the workflow run's logs and failure status for source errors.

The displayed offers are public realtor snapshots, not guaranteed unit inventory. Follow each listing link to confirm current pricing and availability.
