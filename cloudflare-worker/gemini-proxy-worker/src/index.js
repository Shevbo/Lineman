addEventListener('fetch', event => {
  event.respondWith(handleRequest(event.request));
});

async function handleRequest(request) {
  const url = new URL(request.url);
  url.host = 'generativelanguage.googleapis.com'; // Target Gemini API host

  // Reconstruct the request to the Gemini API
  const newRequest = new Request(url.toString(), {
    method: request.method,
    headers: request.headers,
    body: request.body,
    redirect: 'follow'
  });

  // Fetch from Gemini API
  const response = await fetch(newRequest);

  // You might want to add some logging or error handling here
  return response;
}