"""Snake - juego clasico. Generado por SovNode (skeleton local, sin LLM)."""
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
