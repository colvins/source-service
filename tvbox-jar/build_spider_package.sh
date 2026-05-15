#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
TVBOX_DIR="$ROOT/tvbox-jar"
ARTIFACT_DIR="$ROOT/app/artifacts"
OUT_TXT="$ARTIFACT_DIR/colvins-tvbox-spider.txt"
OUT_JAR="$ARTIFACT_DIR/colvins-tvbox-spider.jar"
R8_VERSION=${R8_VERSION:-8.10.24}
R8_JAR=${R8_JAR:-/tmp/r8-${R8_VERSION}.jar}

mkdir -p "$ARTIFACT_DIR"
cd "$TVBOX_DIR"
gradle clean jar --no-daemon >/tmp/gradle-build.log 2>&1 || { cat /tmp/gradle-build.log; exit 1; }
rm -rf /tmp/colvins-stage /tmp/colvins-dex /tmp/colvins-program.jar /tmp/MANIFEST.MF
mkdir -p /tmp/colvins-stage /tmp/colvins-dex
cp -R build/classes/java/main/com /tmp/colvins-stage/
cp -R build/classes/java/stub/android /tmp/colvins-stage/
GSON_JAR=$(find /home/gradle/.gradle /root/.gradle -path '*com.google.code.gson/gson/*/*.jar' 2>/dev/null | head -n 1)
if [ -z "$GSON_JAR" ]; then
  echo 'gson jar not found in gradle cache' >&2
  exit 1
fi
(
  cd /tmp/colvins-stage
  jar xf "$GSON_JAR"
  rm -rf META-INF android/util/Base64.java android/content/Context.java com/github/catvod/crawler || true
  jar cf /tmp/colvins-program.jar .
)
if [ ! -f "$R8_JAR" ]; then
  curl -fsSL -o "$R8_JAR" "https://storage.googleapis.com/r8-releases/raw/${R8_VERSION}/r8.jar"
fi
java -cp "$R8_JAR" com.android.tools.r8.D8 --release --min-api 21 --output /tmp/colvins-dex /tmp/colvins-program.jar
cat > /tmp/MANIFEST.MF <<'EOF'
Manifest-Version: 1.0
Dex-Location: classes.dex
Created-By: dx 1.11

EOF
python3 - <<PY2
from pathlib import Path
import zipfile
out = Path("$OUT_TXT")
root = Path("$TVBOX_DIR")
with zipfile.ZipFile(out, 'w', compression=zipfile.ZIP_STORED) as z:
    z.write('/tmp/MANIFEST.MF', 'META-INF/MANIFEST.MF')
    z.write('/tmp/colvins-dex/classes.dex', 'classes.dex')
    assets = root / 'src/main/resources/assets'
    for path in sorted(assets.rglob('*')):
        if path.is_file():
            z.write(path, path.relative_to(root / 'src/main/resources').as_posix())
PY2
cp "$OUT_TXT" "$OUT_JAR"
python3 - <<PY3
from pathlib import Path
import hashlib, zipfile
for path in [Path("$OUT_TXT"), Path("$OUT_JAR")]:
    data = path.read_bytes()
    print(path.name, hashlib.md5(data).hexdigest(), len(data))
    with zipfile.ZipFile(path) as z:
        print(z.namelist())
PY3
