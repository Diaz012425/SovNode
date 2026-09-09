# Copyright (c) 2026 Stephen Díaz
# SovNode - Local Desktop AI Application
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
Librería local de esqueletos para juegos clásicos muy pedidos (Flappy Bird, Snake, Pong).

ES: El tok/s de decode es aproximadamente constante por token generado en
este hardware, así que la única forma de acelerar de verdad un pedido
genérico ("hace un Flappy Bird y guardalo") no es darle al modelo más
contexto — es generar MENOS tokens de salida. Para un pedido genérico, sin
personalización, el archivo completo ya existe acá local, correcto y
probado: se escribe directo, sin pasar por Ollama, sin gastar tokens de generación.

Cuando el pedido SÍ trae personalización (colores, mecánicas extra, otro
lenguaje/motor, dificultad, multijugador, etc. — ver
_CUSTOMIZATION_SIGNAL_RE) o pide modificar un archivo que ya existe, el
fast-path no aplica y el turno cae al camino normal (generación real vía
LLM). El esqueleto es un atajo para el caso común, nunca un reemplazo del
generador — la decisión de cuándo usarlo es deliberadamente conservadora
(ver match_skeleton): ante la duda, no usar el atajo.

EN: Decode tok/s is roughly constant per generated token on this hardware,
so the only real way to speed up a generic request ("make a Flappy Bird and
save it") isn't giving the model more context — it's generating FEWER
output tokens. For a generic, non-customized request, the complete file
already exists here locally, correct and tested: it's written directly, no
Ollama round-trip, no generation tokens spent.

When the request DOES carry customization (colors, extra mechanics, another
language/engine, difficulty, multiplayer, etc. — see
_CUSTOMIZATION_SIGNAL_RE) or asks to modify an existing file, the fast-path
doesn't apply and the turn falls through to normal LLM generation. The
skeleton is a shortcut for the common case, never a replacement for the
generator — the decision of when to use it is deliberately conservative
(see match_skeleton): when in doubt, skip the shortcut.
"""
from __future__ import annotations

import re
from typing import Optional, TypedDict


class Skeleton(TypedDict):
    key: str
    filename: str
    match: "re.Pattern[str]"
    source: str


_SNAKE_SOURCE = '''"""Snake - juego clasico. Generado por SovNode (skeleton local, sin LLM)."""
import pygame
import random
import sys

pygame.init()

CELL_SIZE = 20
GRID_W, GRID_H = 32, 24
WIDTH, HEIGHT = CELL_SIZE * GRID_W, CELL_SIZE * GRID_H
FPS = 10

BLACK = (10, 10, 10)
GREEN = (60, 220, 90)
DARK_GREEN = (30, 140, 55)
RED = (220, 60, 60)
WHITE = (235, 235, 235)

screen = pygame.display.set_mode((WIDTH, HEIGHT))
pygame.display.set_caption("Snake")
clock = pygame.time.Clock()
font = pygame.font.SysFont("consolas", 24)


def random_food(snake):
    while True:
        pos = (random.randint(0, GRID_W - 1), random.randint(0, GRID_H - 1))
        if pos not in snake:
            return pos


def draw_cell(pos, color):
    x, y = pos
    rect = pygame.Rect(x * CELL_SIZE, y * CELL_SIZE, CELL_SIZE, CELL_SIZE)
    pygame.draw.rect(screen, color, rect)
    pygame.draw.rect(screen, BLACK, rect, 1)


def main():
    snake = [(GRID_W // 2, GRID_H // 2)]
    direction = (1, 0)
    pending_direction = direction
    food = random_food(snake)
    score = 0
    game_over = False

    while True:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                pygame.quit()
                sys.exit()
            if event.type == pygame.KEYDOWN:
                if event.key in (pygame.K_UP, pygame.K_w) and direction != (0, 1):
                    pending_direction = (0, -1)
                elif event.key in (pygame.K_DOWN, pygame.K_s) and direction != (0, -1):
                    pending_direction = (0, 1)
                elif event.key in (pygame.K_LEFT, pygame.K_a) and direction != (1, 0):
                    pending_direction = (-1, 0)
                elif event.key in (pygame.K_RIGHT, pygame.K_d) and direction != (-1, 0):
                    pending_direction = (1, 0)
                elif event.key == pygame.K_r and game_over:
                    snake = [(GRID_W // 2, GRID_H // 2)]
                    direction = (1, 0)
                    pending_direction = direction
                    food = random_food(snake)
                    score = 0
                    game_over = False

        if not game_over:
            direction = pending_direction
            head_x, head_y = snake[0]
            new_head = (head_x + direction[0], head_y + direction[1])

            if (
                new_head[0] < 0 or new_head[0] >= GRID_W
                or new_head[1] < 0 or new_head[1] >= GRID_H
                or new_head in snake
            ):
                game_over = True
            else:
                snake.insert(0, new_head)
                if new_head == food:
                    score += 1
                    food = random_food(snake)
                else:
                    snake.pop()

        screen.fill(BLACK)
        for i, segment in enumerate(snake):
            draw_cell(segment, GREEN if i == 0 else DARK_GREEN)
        draw_cell(food, RED)

        score_surf = font.render(f"Puntaje: {score}", True, WHITE)
        screen.blit(score_surf, (10, 10))

        if game_over:
            msg = font.render("Game Over -- presiona R para reiniciar", True, WHITE)
            screen.blit(msg, (WIDTH // 2 - msg.get_width() // 2, HEIGHT // 2))

        pygame.display.flip()
        clock.tick(FPS)


if __name__ == "__main__":
    main()
'''

_PONG_SOURCE = '''"""Pong - juego clasico de dos jugadores. Generado por SovNode (skeleton local, sin LLM)."""
import pygame
import sys

pygame.init()

WIDTH, HEIGHT = 800, 480
PADDLE_W, PADDLE_H = 14, 90
BALL_SIZE = 16
PADDLE_SPEED = 6
BALL_SPEED_X, BALL_SPEED_Y = 5, 5
FPS = 60

BLACK = (12, 12, 16)
WHITE = (235, 235, 235)

screen = pygame.display.set_mode((WIDTH, HEIGHT))
pygame.display.set_caption("Pong")
clock = pygame.time.Clock()
font = pygame.font.SysFont("consolas", 36)


def reset_ball():
    ball = pygame.Rect(WIDTH // 2 - BALL_SIZE // 2, HEIGHT // 2 - BALL_SIZE // 2, BALL_SIZE, BALL_SIZE)
    vel = [BALL_SPEED_X, BALL_SPEED_Y]
    return ball, vel


def main():
    left = pygame.Rect(30, HEIGHT // 2 - PADDLE_H // 2, PADDLE_W, PADDLE_H)
    right = pygame.Rect(WIDTH - 30 - PADDLE_W, HEIGHT // 2 - PADDLE_H // 2, PADDLE_W, PADDLE_H)
    ball, ball_vel = reset_ball()
    score_left, score_right = 0, 0

    while True:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                pygame.quit()
                sys.exit()

        keys = pygame.key.get_pressed()
        if keys[pygame.K_w] and left.top > 0:
            left.y -= PADDLE_SPEED
        if keys[pygame.K_s] and left.bottom < HEIGHT:
            left.y += PADDLE_SPEED
        if keys[pygame.K_UP] and right.top > 0:
            right.y -= PADDLE_SPEED
        if keys[pygame.K_DOWN] and right.bottom < HEIGHT:
            right.y += PADDLE_SPEED

        ball.x += ball_vel[0]
        ball.y += ball_vel[1]

        if ball.top <= 0 or ball.bottom >= HEIGHT:
            ball_vel[1] *= -1

        if ball.colliderect(left) and ball_vel[0] < 0:
            ball_vel[0] *= -1
        if ball.colliderect(right) and ball_vel[0] > 0:
            ball_vel[0] *= -1

        if ball.left <= 0:
            score_right += 1
            ball, ball_vel = reset_ball()
        elif ball.right >= WIDTH:
            score_left += 1
            ball, ball_vel = reset_ball()

        screen.fill(BLACK)
        pygame.draw.aaline(screen, WHITE, (WIDTH // 2, 0), (WIDTH // 2, HEIGHT))
        pygame.draw.rect(screen, WHITE, left)
        pygame.draw.rect(screen, WHITE, right)
        pygame.draw.ellipse(screen, WHITE, ball)

        score_surf = font.render(f"{score_left}   {score_right}", True, WHITE)
        screen.blit(score_surf, (WIDTH // 2 - score_surf.get_width() // 2, 20))

        pygame.display.flip()
        clock.tick(FPS)


if __name__ == "__main__":
    main()
'''

_FLAPPY_SOURCE = '''"""Flappy Bird - clon simple. Generado por SovNode (skeleton local, sin LLM)."""
import pygame
import random
import sys

pygame.init()

WIDTH, HEIGHT = 400, 600
GRAVITY = 0.45
FLAP_STRENGTH = -8.5
PIPE_SPEED = 3
PIPE_GAP = 160
PIPE_WIDTH = 70
PIPE_INTERVAL_MS = 1400
BIRD_SIZE = 28
FPS = 60
GROUND_HEIGHT = 80

SKY = (78, 192, 202)
GROUND = (222, 184, 112)
PIPE_COLOR = (70, 170, 80)
BIRD_COLOR = (250, 200, 60)
WHITE = (245, 245, 245)
BLACK = (20, 20, 20)

screen = pygame.display.set_mode((WIDTH, HEIGHT))
pygame.display.set_caption("Flappy Bird")
clock = pygame.time.Clock()
font = pygame.font.SysFont("consolas", 32)
small_font = pygame.font.SysFont("consolas", 20)


class Bird:
    def __init__(self):
        self.x = WIDTH // 3
        self.y = HEIGHT // 2
        self.vel = 0.0

    def flap(self):
        self.vel = FLAP_STRENGTH

    def update(self):
        self.vel += GRAVITY
        self.y += self.vel

    def rect(self):
        return pygame.Rect(int(self.x - BIRD_SIZE / 2), int(self.y - BIRD_SIZE / 2), BIRD_SIZE, BIRD_SIZE)

    def draw(self):
        pygame.draw.circle(screen, BIRD_COLOR, (int(self.x), int(self.y)), BIRD_SIZE // 2)
        pygame.draw.circle(screen, BLACK, (int(self.x), int(self.y)), BIRD_SIZE // 2, 2)


class PipePair:
    def __init__(self, x):
        self.x = x
        self.gap_y = random.randint(120, HEIGHT - GROUND_HEIGHT - 120)
        self.scored = False

    def update(self):
        self.x -= PIPE_SPEED

    def top_rect(self):
        return pygame.Rect(int(self.x), 0, PIPE_WIDTH, self.gap_y - PIPE_GAP // 2)

    def bottom_rect(self):
        top = self.gap_y + PIPE_GAP // 2
        return pygame.Rect(int(self.x), top, PIPE_WIDTH, HEIGHT - GROUND_HEIGHT - top)

    def draw(self):
        pygame.draw.rect(screen, PIPE_COLOR, self.top_rect())
        pygame.draw.rect(screen, PIPE_COLOR, self.bottom_rect())
        pygame.draw.rect(screen, BLACK, self.top_rect(), 2)
        pygame.draw.rect(screen, BLACK, self.bottom_rect(), 2)

    def off_screen(self):
        return self.x + PIPE_WIDTH < 0


def main():
    bird = Bird()
    pipes = []
    last_pipe_time = pygame.time.get_ticks()
    score = 0
    game_over = False
    started = False

    while True:
        now = pygame.time.get_ticks()
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                pygame.quit()
                sys.exit()
            flap_key = event.type == pygame.KEYDOWN and event.key == pygame.K_SPACE
            flap_click = event.type == pygame.MOUSEBUTTONDOWN
            if flap_key or flap_click:
                if game_over:
                    bird = Bird()
                    pipes = []
                    last_pipe_time = now
                    score = 0
                    game_over = False
                    started = False
                else:
                    started = True
                    bird.flap()

        if started and not game_over:
            bird.update()

            if now - last_pipe_time > PIPE_INTERVAL_MS:
                pipes.append(PipePair(WIDTH + 10))
                last_pipe_time = now

            for pipe in pipes:
                pipe.update()
            pipes = [p for p in pipes if not p.off_screen()]

            bird_rect = bird.rect()
            if bird.y - BIRD_SIZE / 2 <= 0 or bird.y + BIRD_SIZE / 2 >= HEIGHT - GROUND_HEIGHT:
                game_over = True
            for pipe in pipes:
                if bird_rect.colliderect(pipe.top_rect()) or bird_rect.colliderect(pipe.bottom_rect()):
                    game_over = True
                if not pipe.scored and pipe.x + PIPE_WIDTH < bird.x:
                    pipe.scored = True
                    score += 1

        screen.fill(SKY)
        for pipe in pipes:
            pipe.draw()
        pygame.draw.rect(screen, GROUND, (0, HEIGHT - GROUND_HEIGHT, WIDTH, GROUND_HEIGHT))
        bird.draw()

        score_surf = font.render(str(score), True, WHITE)
        screen.blit(score_surf, (WIDTH // 2 - score_surf.get_width() // 2, 30))

        if not started:
            hint = small_font.render("Click o ESPACIO para volar", True, BLACK)
            screen.blit(hint, (WIDTH // 2 - hint.get_width() // 2, HEIGHT // 2))
        elif game_over:
            hint = small_font.render("Game Over -- click o ESPACIO para reiniciar", True, BLACK)
            screen.blit(hint, (WIDTH // 2 - hint.get_width() // 2, HEIGHT // 2))

        pygame.display.flip()
        clock.tick(FPS)


if __name__ == "__main__":
    main()
'''


SKELETONS: "dict[str, Skeleton]" = {
    "snake": {
        "key": "snake",
        "filename": "snake.py",
        "match": re.compile(r"\bsnake\b", re.IGNORECASE),
        "source": _SNAKE_SOURCE,
    },
    "pong": {
        "key": "pong",
        "filename": "pong.py",
        "match": re.compile(r"\bpong\b", re.IGNORECASE),
        "source": _PONG_SOURCE,
    },
    "flappy_bird": {
        "key": "flappy_bird",
        "filename": "flappy_bird.py",
        "match": re.compile(r"\bflappy\w*(?:\s+bird)?\b", re.IGNORECASE),
        "source": _FLAPPY_SOURCE,
    },
}

# ES: Señales de que el pedido NO es genérico (otro lenguaje/motor, mecánicas
# extra, estética específica, multijugador en red, persistencia, IA, etc.).
# Cualquier match acá saca al pedido del fast-path: mejor una skipeada de más
# (cae al generador real) que un archivo que no cumple lo pedido.
# EN: Signals that the request is NOT generic (another language/engine,
# extra mechanics, specific aesthetics, networked multiplayer, persistence,
# AI, etc.). Any match here bumps the request out of the fast-path: better
# an unnecessary skip (falls to the real generator) than a file that doesn't meet the request.
_CUSTOMIZATION_SIGNAL_RE = re.compile(
    r"\b("
    r"javascript|js|typescript|\bts\b|html5?|css|react|node|"
    r"c\+\+|c#|java\b|rust|godot|unity|unreal|"
    r"web|browser|navegador|online|multijugador|multiplayer|red\b|network|"
    r"servidor|server|base\s+de\s+datos|database|"
    r"power-?up|powerup|obst[aá]culo\w*|nivel(?:es)?|level\w*|"
    r"dificultad|difficulty|modo\s+\w+|game\s+mode|"
    r"puntaje\s+m[aá]ximo|high\s?score|"
    r"sprite\w*|imagen\w*|textura\w*|\bpng\b|\bjpg\b|asset\w*|"
    r"sonido\w*|m[uú]sica|sound\w*|music|"
    r"colores?|azul|rojo|verde\w*|amarillo|rosa|morado|negro|blanco|"
    r"\b3d\b|isom[eé]trico|pixel\s?art|"
    r"\bia\b|inteligencia\s+artificial|\bai\b|\bbot\b"
    r")\b",
    re.IGNORECASE,
)

# ES: Largo razonable para un pedido "básico" típico; un pedido bastante más
# largo casi siempre trae requisitos extra en prosa que el regex de arriba no
# nombra explícitamente — se prefiere el corte conservador antes que arriesgar un archivo incompleto.
# EN: A reasonable length for a typical "basic" request; a much longer one
# almost always carries extra prose requirements the regex above doesn't
# name explicitly — a conservative cutoff beats risking an incomplete file.
_MAX_GENERIC_REQUEST_WORDS = 28


def match_skeleton(user_input: str) -> Optional[Skeleton]:
    """
    ES: ¿Es el pedido candidato a saltar directo a un esqueleto local (cero
    tokens de LLM) en vez de generarlo? Devuelve el Skeleton a copiar tal
    cual, o None si el turno debe caer al camino normal — el llamador es
    responsable de chequear además que el archivo destino no exista todavía
    (esto es solo para creación nueva, nunca para pisar algo ya personalizado).
    EN: Is the request a candidate to jump straight to a local skeleton
    (zero LLM tokens) instead of generating it? Returns the Skeleton to copy
    as-is, or None if the turn should fall through to normal generation —
    the caller is also responsible for checking the target file doesn't
    already exist (this is for new creation only, never for overwriting something already customized).
    """
    text = user_input or ""
    if not text.strip():
        return None
    if _CUSTOMIZATION_SIGNAL_RE.search(text):
        return None
    if len(text.split()) > _MAX_GENERIC_REQUEST_WORDS:
        return None
    for skeleton in SKELETONS.values():
        if skeleton["match"].search(text):
            return skeleton
    return None
