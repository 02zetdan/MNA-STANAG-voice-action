Speak to the Fleet

Maritime sensor data, normalised live, commanded by voice — grounded, confirmed, auditable.

A hackathon prototype that closes the loop between heterogeneous sensor input and coalition-shaped tasking output, with a voice interface in the middle.
What it does
NMEA-0183 sensor data is ingested and normalised in real time into a live world model. An operator queries and tasks the fleet by voice; every utterance is grounded against the world model, confirmed by readback before dispatch, and logged. Outbound task assignments and inbound contact reports are emitted as CATL-shaped JSON envelopes over UDP multicast — the same data-distribution pattern STANAG 4817 deployments use, with the ratified wire format swapped for JSON.