# මේ උපාංගය software developers ලාට විකිණීම — ඇත්තටම අපි විකුණන්නේ මොකක්ද?

මේක මම ඇතුළතින් ලියන දේ: hardware එකයි code එකයි අද කරන්න පුළුවන් දේ, developer කෙනෙක් ඉල්ලන දේ, සහ මම මේක යොමු කරන තැන. මේක market forecast එකක් නෙවෙයි — මට මෙතනින් demand එක මැනගන්න බෑ. ඒත් product එක ගැන මට කියන්න පුළුවන්.

## 1. අපි විකුණන දේ එක වාක්‍යයකින්

**ඔබ සතුව දැනටමත් තියෙන දුරකථන ලයින් එක, API එකක් විදිහට.** මිතුරු දුරකථනයක් ළඟ තියෙන පෙට්ටියක් Bluetooth HFP හරහා call එකට join වෙලා, හඬ දෙපැත්තටම WebSocket එකක් හරහා ඔබේ software එකට stream කරනවා — call records, events සහ console එකක් එක්ක.

Developer කෙනෙක්ට මේක වැදගත් ඇයි:

- **මිනිත්තුවට ගෙවන ගාස්තු නෑ.** Twilio, Vapi, Retell, Bland හැමෝම මිනිත්තුවට ගාස්තු ගන්නවා. මෙතන minutes ටික ඔබේම SIM plan එකේ.
- **නම්බර් port කරන්නේ නෑ, SIP trunk එකක් නෑ, carrier account එකක් නෑ.** දුරකථනය pair කරන්න — call කරන්න.
- **හඬ ඇතුළේම තියාගන්න පුළුවන්.** ඔබේම network එකේ LLM එකකට point කරනවා නම් එකම දත්තයක්වත් එළියට යන්නේ නෑ — cloud voice platform එකකට කවදාවත් දෙන්න බැරි දේ.
- **Hack කරන්න පුළුවන්.** ESP-IDF, open firmware, WebSocket + JSON.

## 2. අවංක තත්ත්වය: තවම විකුණන්න බෑ — හේතුව ඔබ හිතන එක නෙවෙයි

පරතරය **features නෙවෙයි — විශ්වසනීයත්වයයි (reliability) ලේඛනයයි (documentation).** මේ සතියේ මම code එකේ දැකපු දේවල් වලින්:

- Cloud audio path එක **මේ සතියේ පළමු වතාවට** bench එකේ හරි ගියා (දෙපැත්තටම).
- Call එකක් මැද්දේ තත්පර 28ක් කිසිම දෙයක් නොගිය අවස්ථාවක් හම්බුණා. තව uplink එක **පරණ හඬ** යවන defect එකක් — ඒකයි Nuwan කියපු "voice latency".
- අන්තිමට board එක reset වුණේ **power** නිසා (`reset reason: POWERON`) — code එක නිසා නෙවෙයි. PC එකේ USB port එක Wi-Fi + Bluetooth peak වෙලාවට වැටෙනවා.
- **අපිට handset ගැළපීමේ *list* එකක් නෑ — *ප්‍රශ්නයක්* තියෙනවා.** හැම ෆෝන් එකක්ම වෙනස් විදිහට හැසිරෙනවා; අපේම logs ඒකට සාක්ෂි.
- Provisioning නෑ, API keys නෑ, ලියාපු protocol එකක් නෑ, update channel එකක් නෑ, console එකේ call history එක දැනට කැඩිලා.

Dev kit එකක් ගන්න developer කෙනෙක් missing features වලට සමාව දෙනවා. ඒත් විශ්වසනීය නැතිකමට සමාව දෙන්නේ නෑ. එයා ඔබේ firmware එක කියවලා use කරන විදිහ හොයාගන්නෙත් නෑ.

## 3. අනිත් අය අඩුවට ගණන් හදන බාධකය

**Handset ගැළපීම සහ RF විශ්වසනීයත්වය මෙය coding ප්‍රශ්නයක් නොවේ — testing ප්‍රශ්නයක්.** ඒ නිසාම මම "හැම ෆෝන් එකක්ම වැඩ කරනවා" කියන උපාංගයක් නොදානවා. මම දාන්නේ **පැහැදිලිව සීමා කරපු dev kit එකක්**: නම් කරපු handsets, නම් කරපු carriers, පැහැදිලිව කියලා. test results එක්ක විතරයි පුළුල් කරන්නේ.

## 4. ගැනුම්කරුවන් වර්ග තුනක් — මම පළමුව ගන්නේ කවුද

1. **QA / phone system testing** — මිනිස්සු දුරකථනයෙන් call කරලා IVR සහ call flows test කරන තැන්. ඕන දේ: scripted calls, recordings, transcripts, CI එකේ assertions. නීරස, ඒත් budget තියෙන, අපි දැනටමත් හදන දේම ඕන. **ලාභය ගන්න පුළුවන් පළමු තැන.**
2. **Local/private voice agents** — හඬ cloud එකට යවන්න බැරි developers. ශබ්දය වැඩිම market එක, ඒත් cloud අයට "හඬ ඔබේ network එකෙන් එළියට යන්නේ නෑ" කියන්න බෑ.
3. **Hobbyist dev-kit අය** — unit economics හොඳම, ඒත් support cost වැඩිම. Visibility එකට හොඳයි, ලාභයට නරකයි.

(1) ලාභයට, (2) කතාවට (story), (3) marketing එකට.

## 5. Launch කරන්න කලින් අල්ලගන්න ඕන කොන්දේසි පහ

1. Mains current එකෙන්, දෙපැත්තටම, **දෙපාරක් එක දිගට** පිරිසිදු call එකක් — reset නෑ, dead stretches නෑ. මේක නැතුව අනිත් දේවල් වැඩක් නෑ.
2. එක් handset එකක් + carrier එකක් test කරලා ලියපු එකක්, අවංක සීමා (limits) පිටුවක් එක්ක.
3. **තත්පර 60ක** developer flow එකක්: device claim කරනවා → key එකක් ගන්නවා → WebSocket එකක් open කරනවා → ඔබේම TTS එක ඇහෙනවා. මේකට හවසක් යනවා නම් product එකක් නෑ.
4. ලියපු wire protocol එකක්, version එකක් සහ compatibility rule එකක් එක්ක.
5. Payload වැඩේ (µ-law, ඊට පස්සේ pacing) — නරක link එකක් තියෙන කොට හඬ කැබලි වෙන්නේ නැතුව gaps වලට කැඩෙන්න ඕන.

## 6. සල්ලි තියෙන තැන සහ ආරක්ෂාව තියෙන තැන

- **Hardware එක සාමාන්‍ය භාණ්ඩයක්** (ESP32 + Bluetooth module). Kit එකකට margin එකක් තියෙන්න පුළුවන්, ඒත් kit නැවත නැවත ලාභය ගේන්නේ නෑ, dev-kit අයට support ගොඩක් ඕන.
- **නැවත නැවත ලාභය එන්නේ software එකෙන්**: cloud bridge, call records, transcript pipeline, SDK එක. තරඟකරුවන්ගෙන් ආරක්ෂා කරන්න පුළුවන් එකම දේ ඒකයි.
- ඒ නිසා: **firmware එක දීලා experience එක විකුණන්න**, නැත්නම් kit එක ලාභ අඩුවට විකුණලා cloud tier එකට ගාස්තු ගන්න. පෙට්ටියක් print කරන්න කලින් මේක තීරණය කරන්න — මොකද ඊළඟට අපි හදන්න ඕන දේ ඒක අනුව වෙනස් වෙනවා.

## 7. මම ඊළඟට කරන දේ පිළිවෙලට

1. Physical setup එක හදලා, ඔප්පු කරපු පිරිසිදු call එකක් ගන්න — අනිත් හැම දේටම gate එක මේකයි.
2. Console එක අවංක කරන්න (call history වැඩ කරන්න ඕන, transcripts පෙන්නන්න ඕන).
3. Developer ට protocol එකයි quickstart එකයි ලියලා, capability matrix එක සීමා එක්කම publish කරන්න.
4. 5-10 pilot developers ලා දාන්න — QA/testing ගැනුම්කරුවන්ගෙන් පටන් ගන්න, එයාලා සල්ලි දෙන්නේ reliability එකට, novelty එකට නෙවෙයි — එයාලගේ failures වලින් roadmap එක ලියන්න.
