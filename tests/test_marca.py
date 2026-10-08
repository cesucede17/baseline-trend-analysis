"""El icono de PALBE en su cabecera.

Es el mismo que su tarjeta del recibidor, y eso es el objetivo: pulsar la
tarjeta y aterrizar aqui tiene que leerse como un solo movimiento.

Se comprueba la POSICION y no solo que el fichero se nombre. El test que
cuenta los hijos de la cabecera (test_plataforma_link.py) tiene `img` en su
lista de etiquetas vacias, asi que un icono suelto como cuarto hijo del
<header> pasaria ese test **y rompeia la maquetacion**: el grid de .topbar es
`1fr auto 1fr` y la fila se parte.
"""
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent


def test_el_icono_esta_dentro_del_grupo_de_la_marca():
    fuente = (RAIZ / "app_palbe_4.py").read_text(encoding="utf-8")
    cabecera = fuente[fuente.index('<header class="topbar">'):fuente.index("</header>")]

    marca = cabecera.index('class="brand-mark"')
    icono = cabecera.index("/static/img/palbe.png")
    nombre = cabecera.index('class="brand-name"')

    assert marca < icono < nombre, (
        "el icono tiene que ir DENTRO de .brand-mark y antes del nombre; "
        "suelto en la cabecera rompe el grid de tres columnas"
    )


def test_el_fichero_del_icono_existe():
    # Una ruta a un PNG ausente da una cabecera con la imagen rota, y eso no
    # lo ve ningun test que solo mire la cadena.
    png = RAIZ / "static" / "img" / "palbe.png"
    assert png.is_file(), f"falta {png}"
    assert png.stat().st_size > 1024


def test_ya_no_queda_el_hexagono_suelto():
    # BRAND_MARK_SVG se retira al sustituirlo. BRAND_FULL_SVG se queda: es la
    # pantalla de login, que no se toca.
    fuente = (RAIZ / "app_palbe_4.py").read_text(encoding="utf-8")
    assert "BRAND_MARK_SVG" not in fuente
    assert "BRAND_FULL_SVG" in fuente


def test_el_marco_del_icono_no_arrastra_fondo_ni_barniz():
    """Hallazgo de la revision final: .brand-mark se penso para un SVG
    blanco sobre fondo verde (degradado de fondo, sombra interior, y un
    barniz pintado con ::after). El PNG de la cabecera ya trae su propio
    fondo solido y su propio radio, asi que sin este contrato el degradado
    verde asoma en las cuatro esquinas del icono y el barniz se pinta ENCIMA
    de la imagen -- el ::after va despues en el DOM y el <img> no tiene
    z-index. Es un fallo de ojo, no de comportamiento: nada de esto rompe
    ninguna otra suite, asi que sin este test nadie lo detecta hasta que
    alguien mire la cabecera con atencion."""
    css = (RAIZ / "assets" / "palbe.css").read_text(encoding="utf-8")

    con_imagen = css.split(".brand-mark:has(img){")[1].split("}")[0].replace(" ", "")
    assert "background:none" in con_imagen, (
        "el caso con imagen sigue arrastrando el degradado de fondo"
    )
    assert "box-shadow:none" in con_imagen, (
        "el caso con imagen sigue arrastrando la sombra interior"
    )

    barniz = css.split(".brand-mark:has(img)::after{")[1].split("}")[0].replace(" ", "")
    assert "content:none" in barniz, (
        "el barniz del ::after sigue pintandose encima del icono"
    )


def test_el_favicon_es_el_mismo_icono():
    """La pestana del navegador tambien lleva la marca de la herramienta.

    Apuntaba a /static/favicon.png, un fichero aparte que no se parecia a la
    tarjeta del recibidor. Tambora y Bartolo ya lo hacian bien sin querer: su
    favicon apunta al mismo PNG que su cabecera, asi que al sustituir ese
    fichero cambiaron las dos cosas a la vez.

    Son DOS etiquetas --la aplicacion y la pantalla de acceso-- y las dos
    tienen que apuntar al icono: si solo se cambia una, la pestana cambia de
    dibujo al iniciar sesion."""
    fuente = (RAIZ / "app_palbe_4.py").read_text(encoding="utf-8")
    apariciones = fuente.count('<link rel="icon" type="image/png" href="/static/img/palbe.png"/>')
    assert apariciones == 2, (
        f"hay {apariciones} etiquetas de favicon apuntando al icono, y deben ser 2"
    )
    assert "/static/favicon.png" not in fuente, "queda una etiqueta con el favicon viejo"

