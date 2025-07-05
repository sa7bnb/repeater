En komplett simplex repeater (papegojrepeater) med modernt webbgränssnitt för Jumbospot SHARI SA818 radiomoduler. 
Programmet tar emot meddelanden och spelar automatiskt upp dem igen för att utöka räckvidden.

Hårdvara som används

Jumbospot SHARI SA818 - Färdigbyggd radiomodul med integrerat CM108 ljudkort
Raspberry Pi 4
Antenn och strömförsörjning till Pi

Huvudfunktioner
✅ Automatisk repeater - Känner av inkommande signaler (COS) och återsänder
✅ Webbgränssnitt - Fjärrkontroll via webbläsare på http://localhost:5000
✅ Volymkontroll - Justerbara nivåer för inspelning och utsändning
✅ Stations-ID - Automatisk uppspelning med konfigurerbart intervall (mp3 fil)
✅ Realtidsstatistik - Övervakning av aktivitet och drifttid
✅ Pre-buffering - Fångar början av meddelanden för komplett återgivning

1. Install sudo apt install git
2. sudo git clone https://github.com/sa7bnb/repeater.git
3. cd repeater
4. sudo chmod +x install_sa818.sh
5. ./install_sa818.sh

// Sa7bnb - Anders Isaksson
OBS! Detta är under utvekling!!!
