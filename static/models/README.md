# 3Dアバターモデル

リアルモード(3D)用のVRMモデル。`thumbs/` にはVRMメタ情報から抽出した選択用サムネイルを置く。

| ファイル | アプリ内の名前 | 出典 | ライセンス |
|---|---|---|---|
| `avatar_real.vrm` | ひなた | [pixiv/three-vrm](https://github.com/pixiv/three-vrm) サンプル (VRM1_Constraint_Twist_Sample) | [VRM Public License 1.0](https://vrm.dev/licenses/1.0/)、利用許可 everyone |
| `avatar_fem.vrm` | つむぎ | [VRoid Studio サンプル(フェミニン)](https://vroid.pixiv.help/hc/en-us/articles/4402614652569) | CC0 |
| `avatar_masc.vrm` | たける | [VRoid Studio サンプル(マスキュリン)](https://vroid.pixiv.help/hc/en-us/articles/4402614652569) | CC0 |
| `avatar_sample_c.vrm` | れん | [VRoid Studio AvatarSample_C](https://vroid.pixiv.help/hc/en-us/articles/4402394424089) | VRoid利用条件(改変・配布可、商用可、allowedUser=Everyone) |
| `avatar_seed.vrm` | シード | [vrm-c/vrm-specification](https://github.com/vrm-c/vrm-specification) Seed-san (by VirtualCast, Inc.) | [VRM Public License 1.0](https://vrm.dev/licenses/1.0/) |

## モデルの追加・差し替え

1. VRMファイルをこのフォルダに置く(blink / aa / happy などの標準表情プリセットを持つモデルを推奨。VRM 0.x / 1.0 どちらも可)
2. `static/app.js` の `AVATARS` に `mode: "real"` のエントリを追加し、`url` と `thumb` を指定する
3. メタ情報の利用許可(allowedUser)と再配布可否を必ず確認すること
   (例: Meebits のVRMは再配布禁止だったため不採用)
