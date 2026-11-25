function toggleNav() {
    document.getElementById('nav-links').classList.toggle('open');
}

function toggleUserMenu(event) {
    if (event) {
        event.preventDefault();
        event.stopPropagation();
    }
    const menu = event.currentTarget.closest('[data-user-menu]');
    if (!menu) return;
    const isOpen = menu.classList.contains('open');
    document.querySelectorAll('[data-user-menu].open').forEach(function (item) {
        item.classList.remove('open');
        const button = item.querySelector('.user-menu-button');
        if (button) button.setAttribute('aria-expanded', 'false');
    });
    if (!isOpen) {
        menu.classList.add('open');
        event.currentTarget.setAttribute('aria-expanded', 'true');
    }
}

document.addEventListener('click', function (event) {
    document.querySelectorAll('[data-user-menu].open').forEach(function (menu) {
        if (!menu.contains(event.target)) {
            menu.classList.remove('open');
            const button = menu.querySelector('.user-menu-button');
            if (button) button.setAttribute('aria-expanded', 'false');
        }
    });
});
