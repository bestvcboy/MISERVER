# Xiaomi QR protocol adaptation

`qr_connector.py` adapts the QR workflow, nonce, RC4-drop1024 encryption and request signing from [Xiaomi-cloud-tokens-extractor](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor), commit `c4db715dace9806e905153c2977608873e8ab7c9`. It does not import or run the upstream CLI, password login, HTTP server, token export or logging code. Local changes include explicit consent, fixed-device selection, private expiring sessions and restricted verified network transport.

The account-level `/app/home/device_list` endpoint and its four request parameters are protocol references from [hass-xiaomi-miot](https://github.com/al-one/hass-xiaomi-miot/blob/master/custom_components/xiaomi_miot/core/xiaomi_cloud.py) and [micloud](https://github.com/Squachen/micloud/blob/master/micloud/micloud.py). This change does not copy or import either SDK. It uses the existing QR transport and local record validation. It does not use the OAuth API or client identity from Xiaomi's Home Assistant SDK.

MIT License

Copyright (c) 2020 Piotr Machowski

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
