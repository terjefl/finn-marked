// Passkey-seremoniene mot /api/auth/*. Ingen avhengigheter: bare base64url-konvertering
// frem og tilbake mellom JSON-en py_webauthn lager og ArrayBuffer-ene nettleseren vil ha.
(() => {
  const b64uTilBytes = s => {
    const b = atob(s.replace(/-/g, '+').replace(/_/g, '/').padEnd(Math.ceil(s.length / 4) * 4, '='));
    return Uint8Array.from(b, c => c.charCodeAt(0));
  };
  const bytesTilB64u = buf => btoa(String.fromCharCode(...new Uint8Array(buf)))
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');

  const post = async (url, data) => {
    const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                 body: JSON.stringify(data || {}), credentials: 'same-origin' });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || `${r.status} ${r.statusText}`);
    return j;
  };

  async function registrer(opts) {
    if (!window.PublicKeyCredential) throw new Error('Nettleseren støtter ikke passkeys.');
    const { options, utfordring } = await post('/api/auth/registrer/start', opts);
    const pk = JSON.parse(options);
    pk.challenge = b64uTilBytes(pk.challenge);
    pk.user.id = b64uTilBytes(pk.user.id);
    (pk.excludeCredentials || []).forEach(c => { c.id = b64uTilBytes(c.id); });
    const cred = await navigator.credentials.create({ publicKey: pk });
    const svar = {
      id: cred.id, rawId: bytesTilB64u(cred.rawId), type: cred.type,
      response: {
        clientDataJSON: bytesTilB64u(cred.response.clientDataJSON),
        attestationObject: bytesTilB64u(cred.response.attestationObject),
        transports: cred.response.getTransports ? cred.response.getTransports() : [],
      },
      clientExtensionResults: cred.getClientExtensionResults ? cred.getClientExtensionResults() : {},
    };
    return post('/api/auth/registrer/fullfor', { utfordring, credential: svar, passkey_navn: opts.passkey_navn });
  }

  async function logginn() {
    if (!window.PublicKeyCredential) throw new Error('Nettleseren støtter ikke passkeys.');
    const { options, utfordring } = await post('/api/auth/logginn/start');
    const pk = JSON.parse(options);
    pk.challenge = b64uTilBytes(pk.challenge);
    (pk.allowCredentials || []).forEach(c => { c.id = b64uTilBytes(c.id); });
    const cred = await navigator.credentials.get({ publicKey: pk });
    const svar = {
      id: cred.id, rawId: bytesTilB64u(cred.rawId), type: cred.type,
      response: {
        clientDataJSON: bytesTilB64u(cred.response.clientDataJSON),
        authenticatorData: bytesTilB64u(cred.response.authenticatorData),
        signature: bytesTilB64u(cred.response.signature),
        userHandle: cred.response.userHandle ? bytesTilB64u(cred.response.userHandle) : null,
      },
      clientExtensionResults: cred.getClientExtensionResults ? cred.getClientExtensionResults() : {},
    };
    return post('/api/auth/logginn/fullfor', { utfordring, credential: svar });
  }

  // Knytter en knapp til en seremoni. Feil vises i elementet med id «feil».
  function knytt(knapp, handling, videre) {
    const feil = document.getElementById('feil');
    knapp.addEventListener('click', async () => {
      knapp.disabled = true;
      if (feil) feil.textContent = '';
      try {
        const res = await handling();
        window.location.href = (res && res.videre) || videre || '/';
      } catch (e) {
        if (feil) feil.textContent = e.name === 'NotAllowedError' ? 'Avbrutt eller avvist av nettleseren.' : e.message;
        knapp.disabled = false;
      }
    });
  }

  window.passkey = { registrer, logginn, knytt };
})();
