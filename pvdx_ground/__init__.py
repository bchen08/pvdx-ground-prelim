"""PVDX ground software (Brown Space Engineering): the cloud telemetry service between the SatNOGS
Network, the BSE ground station, the Grafana dashboard and the mission-control web app.

Subpackages:
  ingest  - pull demodulated frames from the SatNOGS Network API into SQLite
  decode  - pluggable frame decoders (stand-in satellite via satnogs-decoders, PVDX stub)
  storage - write decoded fields to InfluxDB 2.x (the store behind Grafana)
  publish - hand the latest values to Redis for the web app backend
  api     - HTTP API: telemetry, frames, observations, health and the frame push endpoint
  push    - client for the push endpoint, run on the ground station computer
  serve   - run ingest, decode, publish and the API in one process (pvdx-serve)
"""

__version__ = "0.2.0"
