"""
Fase 4c -- vincula una identidad de Keycloak con un usuario de PALBE.

Uso:
    python link_sso.py <username_de_palbe> <sub_de_keycloak> [email]
    python link_sso.py --list

El sub se saca de Keycloak con:
    kcadm.sh get users -r sge --fields id,username

Es deliberadamente manual: no se cruza por email (esta vacio en 4 de los 5
usuarios) ni se auto-provisiona. Son 5 personas, es trabajo de minutos, y a
cambio nadie entra por accidente.
"""
import sys

import palbe_db


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] == "--list":
        for m in palbe_db.list_sso_mappings():
            print(f"{m['palbe_username']:<12} <- {m['keycloak_sub']}  ({m['linked_at']})")
        return 0

    if len(sys.argv) not in (3, 4):
        print(__doc__)
        return 2

    username, sub = sys.argv[1], sys.argv[2]
    email = sys.argv[3] if len(sys.argv) == 4 else ""

    usuario = palbe_db.get_user_by_username(username)
    if usuario is None:
        print(f"ERROR: no existe el usuario de PALBE '{username}'")
        return 1

    if palbe_db.get_sso_mapping(sub) is not None:
        print(f"ERROR: ese sub ya esta vinculado")
        return 1

    palbe_db.create_sso_mapping(sub, usuario.id, keycloak_username=username,
                                keycloak_email=email)
    print(f"vinculado: {username} (id {usuario.id}) <- {sub}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
