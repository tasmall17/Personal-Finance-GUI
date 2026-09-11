#!/bin/zsh
# Rebuild the standalone local copy from the artifact source.
SRC="${1:?usage: build.sh <source cashflow.html>}"
DIR="${0:A:h}"
{
  cat <<'HEAD'
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  html{color-scheme:light dark}
  body{margin:0}
  img{max-width:100%}
  [hidden]{display:none!important}
</style>
HEAD
  sed -n '1,/^<\/style>$/p' "$SRC"
  printf '</head>\n<body>\n'
  sed -n '/^<\/style>$/,$p' "$SRC" | tail -n +2
  printf '</body>\n</html>\n'
} > "$DIR/index.html"
echo "rebuilt $DIR/index.html ($(wc -c < "$DIR/index.html") bytes)"
