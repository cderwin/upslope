<script setup lang="ts">
import { RouterLink } from 'vue-router'
</script>

<template>
  <div class="container py-5 about">
    <h1 class="h3 mb-4">About Upslope</h1>

    <p class="lead">
      In the Pacific Northwest, the best snow often falls after the storm. As a cold front clears, moist ocean air
      keeps driving into the Cascades and is forced upslope, cooling and squeezing out bands of fresh snow. These
      orographic showers are fickle: one drainage gets a foot while the next stays dry. Upslope tracks the wind,
      moisture, and freezing levels behind them, so you know where it’s piling up.
    </p>

    <h2 class="h5 mt-5">How it works</h2>
    <p>
      Twice a day the University of Washington runs a high-resolution weather model (WRF) over the Pacific
      Northwest and publishes its output as forecast images. Upslope downloads those images and georegisters them:
      reference points tie pixels in each image to real latitude and longitude, and a fitted transform reprojects
      the whole frame onto a web map.
    </p>
    <p>
      The <RouterLink :to="{ name: 'map' }">map</RouterLink> lays each forecast variable over satellite imagery and
      optional 3D terrain, so you can see precipitation, wind and freezing levels against the actual ridgelines and
      drainages. Step or play through the forecast hours to watch showers form and drift behind a front. Times are
      shown in Pacific time, computed from each model run’s start time.
    </p>

    <h2 class="h5 mt-5">Credits</h2>
    <dl class="credits">
      <dt>Cameron Derwin</dt>
      <dd>Built and maintains Upslope.</dd>

      <dt>
        <a href="https://atmos.washington.edu/" target="_blank" rel="noopener">UW Atmospheric Sciences</a>
      </dt>
      <dd>
        Runs the <a href="https://a.atmos.washington.edu/wrfrt/" target="_blank" rel="noopener">UW WRF</a> regional
        forecast model and publishes the forecast imagery that Upslope maps.
      </dd>

      <dt><a href="https://open-meteo.com/" target="_blank" rel="noopener">Open-Meteo</a></dt>
      <dd>Open weather forecast data, planned as an additional forecast source.</dd>

      <dt><a href="https://nwac.us/" target="_blank" rel="noopener">Northwest Avalanche Center (NWAC)</a></dt>
      <dd>
        Avalanche forecasting and mountain weather for the Cascades and Olympics, and an inspiration for this project.
        Always check the NWAC forecast before heading into avalanche terrain.
      </dd>
    </dl>

    <p class="small text-body-secondary mt-5">
      Upslope is an independent project and is not affiliated with or endorsed by the University of Washington,
      Open-Meteo or NWAC. Forecasts are model output and can be wrong.
    </p>
  </div>
</template>

<style scoped>
.about {
  max-width: 44rem;
}

.credits dt {
  font-weight: 600;
}

.credits dd {
  margin-bottom: 1rem;
  color: var(--bs-secondary-color);
}
</style>
