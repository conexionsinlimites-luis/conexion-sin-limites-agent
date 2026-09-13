  // Mostrar error si viene ?error=1 en la URL
  if (location.search.includes('error=1')) {
    document.getElementById('err').classList.add('show');
  }
